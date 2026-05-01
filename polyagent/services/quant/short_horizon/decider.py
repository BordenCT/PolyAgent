"""Registry-aware decider for short-horizon binary up/down markets.

For each active market row:
- Look up the AssetSpec from the registry by ``asset_id``.
- Pull the matching :class:`PriceSource`.
- Compute vol via :func:`compute_vol`.
- Run the lognormal estimator and compare against the book mid to derive edge.
- Reject any candidate whose absolute edge is below ``spec.edge_threshold`` or
  whose gross edge does not clear assumed fees.
- Insert a paper trade whenever the candidate clears all gates.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

from polyagent.models import QuantShortTrade
from polyagent.services.quant.assets.registry import apply_env_overrides, get
from polyagent.services.quant.assets.sources.base import PriceSource
from polyagent.services.quant.core.estimator import estimate_up_probability
from polyagent.services.quant.core.vol import compute_vol

logger = logging.getLogger("polyagent.services.quant.short_horizon.decider")


class BookFetcher(Protocol):
    """Reads top-of-book bid/ask for a Polymarket CLOB token."""

    def fetch_mid(self, token_id: str) -> tuple[Decimal, Decimal] | None: ...


class _SettlementSource(Protocol):
    """Fetches historical spot price at a given timestamp."""

    def price_at(self, ts: datetime) -> Decimal | None: ...


class _RepoLike(Protocol):
    def get_trades_for_market(self, market_id: str) -> list[dict]: ...
    def insert_trade(self, trade) -> None: ...
    def count_open_trades_for_asset(self, asset_id: str) -> int: ...
    def set_start_spot(self, market_id: str, start_spot: Decimal) -> None: ...


class QuantDecider:
    """Decide whether to open a paper position on a short-horizon market.

    Args:
        sources: Mapping from ``asset_id`` to its live :class:`PriceSource`.
        book: Order-book fetcher returning ``(best_bid, best_ask)``.
        repo: Repository providing ``get_trades_for_market``, ``insert_trade``,
            and ``count_open_trades_for_asset``.
        position_size_usd: Notional USD per paper trade.
        max_trades_per_cycle: Hard cap on trades opened in a single
            scan-and-decide pass. Prevents the cascade where one Coinbase
            tick triggers identical signals on every active market in the
            same instant. Reset via ``reset_cycle()`` between scans.
        max_open_per_asset: Hard cap on simultaneously-open paper trades
            per ``asset_id``. All short-horizon trades on the same asset
            in the same orchestrator pass are 100%% correlated (same
            spot, same vol, same model output), so the cap also bounds
            correlated paper-bankroll exposure.
    """

    def __init__(
        self,
        sources: dict[str, PriceSource],
        book: BookFetcher,
        repo: _RepoLike,
        position_size_usd: Decimal,
        max_trades_per_cycle: int = 5,
        max_open_per_asset: int = 3,
        settlements: dict[str, _SettlementSource] | None = None,
    ) -> None:
        self._sources = sources
        self._book = book
        self._repo = repo
        self._size = position_size_usd
        self._max_per_cycle = max_trades_per_cycle
        self._max_open_per_asset = max_open_per_asset
        self._opened_this_cycle = 0
        self._settlements: dict[str, _SettlementSource] = settlements or {}

    def reset_cycle(self) -> None:
        """Reset the per-cycle trade counter. Call at the start of each scan."""
        self._opened_this_cycle = 0

    def evaluate(self, market_row: dict) -> None:
        """Evaluate one market row and persist a paper trade if it clears gates.

        Skip conditions, in order:
        - per-cycle trade cap reached,
        - market already has a trade (silent: expected, dominates the log),
        - no spec registered for ``asset_id``,
        - per-asset open-position cap reached,
        - no live price source for ``asset_id`` or no current spot,
        - window already closed,
        - window has not opened yet (Polymarket lists hours in advance),
        - no book mid available,
        - absolute edge below ``spec.edge_threshold``,
        - gross edge does not exceed assumed fees.

        Every skip except ``already_traded`` emits a one-line INFO log
        prefixed with ``SKIP <slug> reason=<code>``, plus a few key=value
        diagnostic fields, so the operator can grep by reason to see why
        the bot isn't entering markets:

            grep SKIP output.log | grep "reason=open_cap"
        """
        slug = market_row.get("slug") or market_row.get("polymarket_id") or "?"

        if self._opened_this_cycle >= self._max_per_cycle:
            self._log_skip(slug, "cycle_cap",
                           opened=self._opened_this_cycle,
                           limit=self._max_per_cycle)
            return

        market_id = market_row["id"]
        if self._repo.get_trades_for_market(market_id):
            # Intentionally silent. Once a market has any trade row, we
            # never re-enter; logging this on every cycle would drown
            # the more interesting skip reasons.
            return

        asset_id = market_row.get("asset_id") or "BTC"
        base_spec = get(asset_id)
        if base_spec is None:
            logger.warning("no spec for asset_id=%s, skipping market %s", asset_id, market_id)
            self._log_skip(slug, "no_spec", asset=asset_id)
            return
        spec = apply_env_overrides(base_spec)

        open_count = self._repo.count_open_trades_for_asset(asset_id)
        if open_count >= self._max_open_per_asset:
            self._log_skip(slug, "open_cap",
                           asset=asset_id,
                           open=open_count,
                           limit=self._max_open_per_asset)
            return

        source = self._sources.get(asset_id)
        if source is None:
            self._log_skip(slug, "no_source", asset=asset_id)
            return
        spot = source.current()
        if spot is None:
            self._log_skip(slug, "no_spot", asset=asset_id)
            return

        window_start = market_row["window_start_ts"]
        window_end = market_row["window_end_ts"]
        now = datetime.now(timezone.utc)
        ttm = (window_end - now).total_seconds()
        if ttm <= 0:
            self._log_skip(slug, "window_closed", ttm=f"{ttm:.0f}")
            return
        # Polymarket lists short-horizon markets hours before their windows
        # open. Without this guard the decider would enter on a market with
        # no signal yet (start_spot fetched from a future timestamp returns
        # garbage or None) and the trade would sit in the cap for the full
        # listing-to-resolution span (often 9+ hours), starving live windows.
        secs_until_open = (window_start - now).total_seconds()
        if secs_until_open > 0:
            self._log_skip(slug, "window_not_open",
                           minutes_until_open=f"{secs_until_open / 60:.1f}")
            return

        start_spot_raw = market_row.get("start_spot")
        if start_spot_raw is None:
            settlement = self._settlements.get(asset_id)
            if settlement is not None:
                fetched = settlement.price_at(market_row["window_start_ts"])
                if fetched is not None:
                    self._repo.set_start_spot(market_id, fetched)
                    start_spot_raw = fetched
        start_spot = Decimal(str(start_spot_raw)) if start_spot_raw is not None else spot

        vol = compute_vol(spec, source, horizon_s=ttm)
        p_up = estimate_up_probability(start_spot, spot, ttm, vol)

        book = self._book.fetch_mid(market_row["token_id_yes"])
        if book is None:
            self._log_skip(slug, "no_book", token=market_row["token_id_yes"])
            return
        bid, ask = book
        mid = (float(bid) + float(ask)) / 2.0

        edge = p_up - mid
        if abs(edge) < spec.edge_threshold:
            self._log_skip(slug, "edge_below_threshold",
                           edge=f"{edge:+.4f}",
                           threshold=f"{spec.edge_threshold:.4f}",
                           p_up=f"{p_up:.4f}",
                           mid=f"{mid:.4f}")
            return

        size_fraction = float(self._size)
        gross_edge_usd = abs(edge) * size_fraction
        fees_usd = size_fraction * spec.fee_bps / 10_000.0
        if gross_edge_usd <= fees_usd:
            self._log_skip(slug, "fees_above_edge",
                           gross_edge=f"{gross_edge_usd:.4f}",
                           fees=f"{fees_usd:.4f}")
            return

        if edge > 0:
            side, fill = "YES", ask
        else:
            side, fill = "NO", bid

        trade = QuantShortTrade(
            market_id=market_id,
            side=side,
            fill_price_assumed=fill,
            size=self._size,
            estimator_p_up=p_up,
            spot_at_decision=spot,
            vol_at_decision=vol,
            edge_at_decision=edge,
        )
        self._repo.insert_trade(trade)
        self._opened_this_cycle += 1
        logger.info(
            "PAPER %s side=%s edge=%+.4f p_up=%.4f mid=%.4f asset=%s",
            slug, side, edge, p_up, mid, asset_id,
        )

    @staticmethod
    def _log_skip(slug: str, reason: str, **fields) -> None:
        """Emit ``SKIP <slug> reason=<code> [k=v ...]`` for the operator.

        Single line per skip so ``grep SKIP`` is the natural read pattern.
        ``grep "reason=<code>"`` filters by gate.
        """
        if fields:
            extra = " ".join(f"{k}={v}" for k, v in fields.items())
            logger.info("SKIP %s reason=%s %s", slug, reason, extra)
        else:
            logger.info("SKIP %s reason=%s", slug, reason)

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
        - market already has a trade,
        - no spec registered for ``asset_id``,
        - per-asset open-position cap reached,
        - no live price source for ``asset_id`` or no current spot,
        - window already closed,
        - no book mid available,
        - absolute edge below ``spec.edge_threshold``,
        - gross edge does not exceed assumed fees.
        """
        if self._opened_this_cycle >= self._max_per_cycle:
            return

        market_id = market_row["id"]
        if self._repo.get_trades_for_market(market_id):
            return

        asset_id = market_row.get("asset_id") or "BTC"
        base_spec = get(asset_id)
        if base_spec is None:
            logger.warning("no spec for asset_id=%s, skipping market %s", asset_id, market_id)
            return
        spec = apply_env_overrides(base_spec)

        if self._repo.count_open_trades_for_asset(asset_id) >= self._max_open_per_asset:
            return

        source = self._sources.get(asset_id)
        if source is None:
            return
        spot = source.current()
        if spot is None:
            return

        window_end = market_row["window_end_ts"]
        now = datetime.now(timezone.utc)
        ttm = (window_end - now).total_seconds()
        if ttm <= 0:
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
            return
        bid, ask = book
        mid = (float(bid) + float(ask)) / 2.0

        edge = p_up - mid
        if abs(edge) < spec.edge_threshold:
            return

        size_fraction = float(self._size)
        gross_edge_usd = abs(edge) * size_fraction
        fees_usd = size_fraction * spec.fee_bps / 10_000.0
        if gross_edge_usd <= fees_usd:
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
            "PAPER %s on %s: side=%s edge=%+.3f p_up=%.3f mid=%.3f",
            asset_id, market_row["polymarket_id"], side, edge, p_up, mid,
        )

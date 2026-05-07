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
from decimal import Decimal, ROUND_CEILING
from typing import Callable, Optional, Protocol

from polyagent.models import QuantShortTrade
from polyagent.services.bankroll import BankrollState
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
    # Optional analytics methods. Older test repos may not implement these;
    # the decider checks via hasattr so legacy paths stay green.
    def insert_rejection(self, **fields) -> None: ...
    def count_recent_trades_for_asset(self, asset_id: str, seconds: int) -> int: ...


class QuantDecider:
    """Decide whether to open a paper position on a short-horizon market.

    Args:
        sources: Mapping from ``asset_id`` to its live :class:`PriceSource`.
        book: Order-book fetcher returning ``(best_bid, best_ask)``.
        repo: Repository providing ``get_trades_for_market``, ``insert_trade``,
            and ``count_open_trades_for_asset``.
        position_size_usd: Maximum USD notional per paper trade. With
            ``bankroll_provider`` set this is a *cap* on the Kelly-sized
            position; without it, this is the literal fixed size used.
        max_trades_per_cycle: Hard cap on trades opened in a single
            scan-and-decide pass. Prevents the cascade where one Coinbase
            tick triggers identical signals on every active market in the
            same instant. Reset via ``reset_cycle()`` between scans.
        max_open_per_asset: Hard cap on simultaneously-open paper trades
            per ``asset_id``. All short-horizon trades on the same asset
            in the same orchestrator pass are 100%% correlated (same
            spot, same vol, same model output), so the cap also bounds
            correlated paper-bankroll exposure.
        bankroll_provider: Optional callable returning the current unified
            :class:`BankrollState`. When provided, the decider:

            - Skips a candidate if ``state.free`` is below
              ``min_free_bankroll``, logging ``reason=bankroll_floor``.
            - Sizes the position via Kelly:
              ``size = min(|edge| * kelly_max_fraction * state.free,
                          position_size_usd, state.free - min_free_bankroll)``,
              floored at $0.01.

            When None, every accepted candidate gets the fixed
            ``position_size_usd`` (legacy behavior, preserved for tests).
        kelly_max_fraction: Kelly-fraction multiplier when sizing
            against bankroll. 0.25 (quarter Kelly) is the standard
            conservative paper default.
        min_free_bankroll: Floor below which the decider refuses to
            enter regardless of edge, mirroring the main bot's
            ``MIN_FREE_BANKROLL`` gate.
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
        bankroll_provider: Optional[Callable[[], BankrollState]] = None,
        kelly_max_fraction: float = 0.25,
        min_free_bankroll: Decimal = Decimal("1.0"),
        min_contracts: int = 1,
        use_constant_predictor: bool = False,
    ) -> None:
        self._sources = sources
        self._book = book
        self._repo = repo
        self._size = position_size_usd
        self._max_per_cycle = max_trades_per_cycle
        self._max_open_per_asset = max_open_per_asset
        self._opened_this_cycle = 0
        self._settlements: dict[str, _SettlementSource] = settlements or {}
        self._bankroll_provider = bankroll_provider
        self._kelly_max_fraction = float(kelly_max_fraction)
        self._min_free_bankroll = Decimal(str(min_free_bankroll))
        self._min_contracts = max(1, int(min_contracts))
        self._use_constant_predictor = bool(use_constant_predictor)

    def update_thresholds(
        self,
        position_size_usd: Decimal | None = None,
        max_trades_per_cycle: int | None = None,
        max_open_per_asset: int | None = None,
        kelly_max_fraction: float | None = None,
        min_free_bankroll: Decimal | None = None,
        min_contracts: int | None = None,
        use_constant_predictor: bool | None = None,
    ) -> None:
        """Hot-reload sizing/cap knobs from a fresh .env without restart.

        Mirrors :meth:`ExecutorService.update_thresholds`. Each kwarg is
        optional; None leaves the existing value in place.
        """
        if position_size_usd is not None:
            self._size = position_size_usd
        if max_trades_per_cycle is not None:
            self._max_per_cycle = max_trades_per_cycle
        if max_open_per_asset is not None:
            self._max_open_per_asset = max_open_per_asset
        if kelly_max_fraction is not None:
            self._kelly_max_fraction = float(kelly_max_fraction)
        if min_free_bankroll is not None:
            self._min_free_bankroll = Decimal(str(min_free_bankroll))
        if min_contracts is not None:
            self._min_contracts = max(1, int(min_contracts))
        if use_constant_predictor is not None:
            self._use_constant_predictor = bool(use_constant_predictor)

    def reset_cycle(self) -> None:
        """Reset the per-cycle trade counter. Call at the start of each scan."""
        self._opened_this_cycle = 0

    @property
    def opened_this_cycle(self) -> int:
        """Number of trades inserted since the last ``reset_cycle()``."""
        return self._opened_this_cycle

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
                           market_row=market_row,
                           opened=self._opened_this_cycle,
                           limit=self._max_per_cycle)
            return

        market_id = market_row["id"]
        if self._repo.get_trades_for_market(market_id):
            # Persist this skip to the rejections table so the full
            # scanner-to-decider funnel is visible in SQL, but suppress
            # the log line: at trade cadence this fires on every cycle
            # and would drown the more interesting reasons in the log.
            # Filter SQL queries with ``WHERE reason != 'already_traded'``
            # when you want the post-acceptance gate breakdown.
            if hasattr(self._repo, "insert_rejection"):
                try:
                    self._repo.insert_rejection(
                        reason="already_traded",
                        slug=slug,
                        polymarket_id=market_row.get("polymarket_id"),
                        asset_id=market_row.get("asset_id"),
                    )
                except Exception:
                    logger.exception("failed to persist rejection for %s", slug)
            return

        asset_id = market_row.get("asset_id") or "BTC"
        base_spec = get(asset_id)
        if base_spec is None:
            logger.warning("no spec for asset_id=%s, skipping market %s", asset_id, market_id)
            self._log_skip(slug, "no_spec", market_row=market_row, asset=asset_id)
            return
        spec = apply_env_overrides(base_spec)

        open_count = self._repo.count_open_trades_for_asset(asset_id)
        if open_count >= self._max_open_per_asset:
            self._log_skip(slug, "open_cap",
                           market_row=market_row,
                           asset=asset_id,
                           open=open_count,
                           limit=self._max_open_per_asset)
            return

        source = self._sources.get(asset_id)
        if source is None:
            self._log_skip(slug, "no_source", market_row=market_row, asset=asset_id)
            return
        spot = source.current()
        if spot is None:
            self._log_skip(slug, "no_spot", market_row=market_row, asset=asset_id)
            return

        window_start = market_row["window_start_ts"]
        window_end = market_row["window_end_ts"]
        now = datetime.now(timezone.utc)
        ttm = (window_end - now).total_seconds()
        if ttm <= 0:
            self._log_skip(slug, "window_closed",
                           market_row=market_row, ttm=f"{ttm:.0f}")
            return
        # Polymarket lists short-horizon markets hours before their windows
        # open. Without this guard the decider would enter on a market with
        # no signal yet (start_spot fetched from a future timestamp returns
        # garbage or None) and the trade would sit in the cap for the full
        # listing-to-resolution span (often 9+ hours), starving live windows.
        secs_until_open = (window_start - now).total_seconds()
        if secs_until_open > 0:
            self._log_skip(slug, "window_not_open",
                           market_row=market_row,
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
            self._log_skip(slug, "no_book",
                           market_row=market_row,
                           token=market_row["token_id_yes"])
            return
        bid, ask = book
        mid = (float(bid) + float(ask)) / 2.0

        edge = p_up - mid
        if abs(edge) < spec.edge_threshold:
            self._log_skip(slug, "edge_below_threshold",
                           market_row=market_row,
                           abs_edge=abs(edge),
                           p_up=p_up,
                           mid=mid,
                           vol=vol,
                           spot=spot,
                           edge=f"{edge:+.4f}",
                           threshold=f"{spec.edge_threshold:.4f}")
            return

        # Side determination. The default policy uses the lognormal
        # estimator's directional view (sign of `edge = p_up - mid`).
        #
        # When ``use_constant_predictor`` is set, the side is chosen by
        # fading the market mid instead: bet YES if mid<0.5, NO otherwise,
        # and recompute the sizing edge against 0.5. Selection (the
        # |edge|>threshold gate above) still uses the original Phi(d2)
        # p_up, so the trade universe is unchanged from the standard
        # policy. This isolates the predictor's *direction* call as the
        # single variable being changed, mirroring the disagreement-only
        # counterfactual that motivated the flag.
        if self._use_constant_predictor:
            sizing_edge = 0.5 - mid
        else:
            sizing_edge = edge

        if sizing_edge > 0:
            side, fill = "YES", ask
        else:
            side, fill = "NO", (Decimal("1") - bid)

        # Bankroll floor + Kelly sizing. Kept out of the early-gate block
        # so that fee-of-edge checks don't run when bankroll has decided
        # we can't afford anything anyway. Decoupled from the cap check
        # because a tight bankroll is a stricter constraint than the
        # simultaneous-trades cap.
        # Sizing, fees, and stored edge_at_decision all use the *decision*
        # edge — i.e., ``sizing_edge`` — so that the data row faithfully
        # records the predictor that drove the trade, not the legacy
        # Phi(d2) view. ``estimator_p_up`` still records the legacy view
        # for post-hoc comparison.
        size = self._compute_size(sizing_edge, slug, fill)
        if size is None:
            return  # already logged

        size_fraction = float(size)
        gross_edge_usd = abs(sizing_edge) * size_fraction
        fees_usd = size_fraction * spec.fee_bps / 10_000.0
        if gross_edge_usd <= fees_usd:
            self._log_skip(slug, "fees_above_edge",
                           market_row=market_row,
                           abs_edge=abs(sizing_edge),
                           p_up=p_up,
                           mid=mid,
                           fill_price=fill,
                           vol=vol,
                           spot=spot,
                           gross_edge=f"{gross_edge_usd:.4f}",
                           fees=f"{fees_usd:.4f}")
            return

        # Predicted EV: signed edge times notional. Realised EV (= pnl)
        # is stamped at resolve time; the gap between this and pnl on
        # resolved rows is the cleanest single number for "did the edge
        # model translate into money."
        size_float = float(size)
        predicted_ev = Decimal(str(round(abs(sizing_edge) * size_float, 4)))

        # Spot-trajectory snapshot from the in-memory tick buffer. Pure
        # reads, no network. None when buffer doesn't span the lookback.
        return_5m = return_15m = return_30m = None
        realized_vol_5m = None
        if hasattr(source, "recent_return"):
            return_5m = source.recent_return(seconds_back=300)
            return_15m = source.recent_return(seconds_back=900)
            return_30m = source.recent_return(seconds_back=1800)
        if hasattr(source, "realized_vol"):
            realized_vol_5m = source.realized_vol(window_s=300)

        # Concurrency flag: another trade on this asset within the last
        # 60s suggests a clustered (correlated) decision. Calibration
        # analysis can use this to weight independent samples differently.
        concurrent_with_prior = False
        if hasattr(self._repo, "count_recent_trades_for_asset"):
            try:
                concurrent_with_prior = self._repo.count_recent_trades_for_asset(
                    asset_id, seconds=60,
                ) > 0
            except Exception:
                logger.exception("count_recent_trades_for_asset failed for %s", asset_id)

        trade = QuantShortTrade(
            market_id=market_id,
            side=side,
            fill_price_assumed=fill,
            size=size,
            estimator_p_up=p_up,
            spot_at_decision=spot,
            vol_at_decision=vol,
            edge_at_decision=sizing_edge,
            predicted_ev=predicted_ev,
            return_5m=return_5m,
            return_15m=return_15m,
            return_30m=return_30m,
            realized_vol_5m=realized_vol_5m,
            concurrent_with_prior=concurrent_with_prior,
        )
        self._repo.insert_trade(trade)
        self._opened_this_cycle += 1
        contracts = float(size) / float(fill) if float(fill) > 0 else 0.0
        logger.info(
            "PAPER %s side=%s edge=%+.4f p_up=%.4f mid=%.4f size=$%.2f contracts=%.2f asset=%s",
            slug, side, sizing_edge, p_up, mid, float(size), contracts, asset_id,
        )

    def _compute_size(self, edge: float, slug: str, fill: Decimal) -> Decimal | None:
        """Return the position size to insert, or None to skip.

        Returns None and logs a SKIP when the bankroll provider says the
        free balance is below the floor. Without a bankroll provider the
        legacy fixed-size behavior is preserved (used by tests and as a
        safe default if the wiring isn't set up).

        ``fill`` is the per-contract price; the final size is always an
        integer multiple of it because Polymarket trades whole contracts.
        """
        if self._bankroll_provider is None:
            return self._enforce_integer_contracts(self._size, fill, slug, headroom=None)
        state = self._bankroll_provider()
        free = state.free
        if free < self._min_free_bankroll:
            self._log_skip(slug, "bankroll_floor",
                           free=f"{free:.2f}",
                           floor=f"{self._min_free_bankroll:.2f}",
                           open=f"{state.open_capital_total:.2f}")
            return None
        kelly_dollars = Decimal(str(abs(edge) * self._kelly_max_fraction)) * free
        headroom = free - self._min_free_bankroll
        size = min(kelly_dollars, self._size, headroom)
        if size < Decimal("0.01"):
            self._log_skip(slug, "bankroll_floor",
                           free=f"{free:.2f}",
                           kelly=f"{kelly_dollars:.4f}",
                           note="size_under_one_cent")
            return None
        return self._enforce_integer_contracts(size, fill, slug, headroom=headroom)

    def _enforce_integer_contracts(
        self,
        size: Decimal,
        fill: Decimal,
        slug: str,
        headroom: Decimal | None,
    ) -> Decimal | None:
        """Round USD notional UP to a whole-contract amount.

        Polymarket only fills integer-contract orders. We ceil rather
        than floor so YES/NO sizing is symmetric across the order-book
        bid/ask asymmetry. (The previous floor + flat-$1 bump created a
        cliff at every clean divisor of $1: at fill=0.500 the bump-then-
        floor pipeline produced 2 contracts; at fill=0.501 it produced
        1, halving the stake. Because YES bids park at exactly 0.500 in
        balanced binary markets and YES asks sit a tick above, NO trades
        landed on the favourable side of the cliff far more often, which
        showed up as a 32% NO stake premium with no Kelly justification.)

        When the ceil overshoots the headroom, fall back to the largest
        whole-contract amount that fits. ``headroom=None`` is the legacy
        no-bankroll-provider path and assumes unbounded headroom.
        """
        if fill <= 0:
            self._log_skip(slug, "degenerate_fill", fill=f"{fill}")
            return None
        contracts = int((size / fill).to_integral_value(rounding=ROUND_CEILING))
        contracts = max(contracts, self._min_contracts)
        notional = Decimal(contracts) * fill
        if headroom is not None and notional > headroom:
            # Ceiling overshoots the bankroll floor; fall back to floor
            # against the headroom (strict cap). If even min_contracts
            # cannot fit, skip.
            contracts = int(headroom / fill)
            if contracts < self._min_contracts:
                min_notional = Decimal(self._min_contracts) * fill
                self._log_skip(slug, "below_min_contracts",
                               size=f"{size:.4f}",
                               fill=f"{fill:.4f}",
                               min=str(self._min_contracts),
                               need=f"{min_notional:.4f}",
                               headroom=f"{headroom:.2f}")
                return None
            notional = Decimal(contracts) * fill
        return notional.quantize(Decimal("0.01"))

    # Diagnostic fields the rejections table promotes to first-class
    # columns. Anything else passed to ``_log_skip(**fields)`` lands in
    # the JSONB ``extra`` column instead.
    _REJECTION_NUMERIC_FIELDS = {
        "abs_edge", "p_up", "mid", "fill_price", "vol", "spot",
    }

    def _log_skip(
        self,
        slug: str,
        reason: str,
        *,
        market_row: dict | None = None,
        **fields,
    ) -> None:
        """Emit ``SKIP <slug> reason=<code>`` and persist a rejection row.

        Single line per skip so ``grep SKIP`` is the natural read pattern.
        ``grep "reason=<code>"`` filters by gate. The same data is also
        written to ``quant_decider_rejections`` when the repo supports
        it, so post-hoc analysis can look at the considered-but-skipped
        pool the same way it looks at trades.

        Args:
            slug: Market slug for the log line.
            reason: Reason code (the same string used in log output).
            market_row: Market row dict; used to populate polymarket_id
                and asset_id on the persisted rejection. Optional so
                older fixtures keep working.
            **fields: Diagnostic key/value pairs. Numeric ones with names
                in :data:`_REJECTION_NUMERIC_FIELDS` are promoted to
                dedicated columns; everything else is bundled into the
                JSONB ``extra`` column.
        """
        if fields:
            extra_log = " ".join(f"{k}={v}" for k, v in fields.items())
            logger.info("SKIP %s reason=%s %s", slug, reason, extra_log)
        else:
            logger.info("SKIP %s reason=%s", slug, reason)

        if not hasattr(self._repo, "insert_rejection"):
            return

        promoted: dict[str, float | None] = {}
        extra: dict[str, str] = {}
        for key, value in fields.items():
            if key in self._REJECTION_NUMERIC_FIELDS:
                promoted[key] = self._coerce_numeric(value)
            else:
                extra[key] = str(value)

        try:
            self._repo.insert_rejection(
                reason=reason,
                slug=slug,
                polymarket_id=(market_row or {}).get("polymarket_id"),
                asset_id=(market_row or {}).get("asset_id"),
                abs_edge=promoted.get("abs_edge"),
                p_up=promoted.get("p_up"),
                mid=promoted.get("mid"),
                fill_price=promoted.get("fill_price"),
                vol=promoted.get("vol"),
                spot=promoted.get("spot"),
                extra=extra or None,
            )
        except Exception:
            # Persisting a rejection must never crash the decider; the
            # log line above is the durable fallback.
            logger.exception("failed to persist rejection for %s", slug)

    @staticmethod
    def _coerce_numeric(value) -> float | None:
        """Best-effort numeric coercion for promoted rejection fields.

        Some gates pass formatted strings ("+0.0123"); strip and parse.
        Returns None when the value isn't interpretable as a float so
        the rejection still inserts (the column accepts NULL).
        """
        if value is None:
            return None
        if isinstance(value, (int, float, Decimal)):
            return float(value)
        try:
            return float(str(value).lstrip("+"))
        except (TypeError, ValueError):
            return None

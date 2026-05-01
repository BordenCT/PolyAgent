"""Source-of-truth resolver for short-horizon binary markets.

Reads unresolved markets whose window has closed, queries Polymarket's
CLOB for the actual settled outcome, stamps that outcome on the market
row, and updates each linked trade's realized P&L.

History note: an earlier version computed the outcome by comparing
``start_spot`` to ``end_spot`` from a registered :class:`SettlementSource`.
Cross-checking with ``polyagent quant-validate`` showed ~43% disagreement
with Polymarket's actual settlements, driven by reference-price drift
between our settlement source (e.g. Coinbase) and Polymarket's
(typically Binance via UMA). The bias was consistently positive,
recording phantom wins. The fix is to defer to Polymarket: their
``winner`` flag on a token is the only authoritative answer.

We still record ``start_spot`` and ``end_spot`` from the settlement
source for diagnostic value (so the bot's prediction signal can be
audited against actual price drift), but the outcome no longer depends
on them. Markets where Polymarket has not yet resolved (UMA's 2 to 3
hour settlement lag) are skipped on this pass and retried on the next.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Protocol

from polyagent.services.quant.assets.sources.base import SettlementSource
from polyagent.services.quant.core.pnl import compute_pnl

logger = logging.getLogger("polyagent.services.quant.short_horizon.resolver")

_PM_PRICE_SOURCE_ID = "polymarket:clob"


class _RepoLike(Protocol):
    def get_unresolved_markets_past_end(self, now: datetime) -> list[dict]: ...
    def update_market_resolution(
        self,
        market_id: str,
        *,
        start_spot,
        end_spot,
        outcome: str,
        price_source_id: str,
    ) -> None: ...
    def get_trades_for_market(self, market_id: str) -> list[dict]: ...
    def update_trade_pnl(self, trade_id: str, pnl) -> None: ...


class _ClientLike(Protocol):
    """Subset of :class:`PolymarketClient` the resolver depends on."""

    def fetch_market_state(self, condition_id: str) -> Optional[dict]: ...


class QuantResolver:
    """Resolve short-horizon markets and back-fill paper-trade P&L.

    Args:
        repo: Repository providing the read/update methods declared in
            :class:`_RepoLike`.
        client: Polymarket client used to fetch each market's authoritative
            settled outcome.
        settlements: Mapping from ``asset_id`` to its
            :class:`SettlementSource`. Used only to record ``start_spot``
            and ``end_spot`` for diagnostic audit; the *outcome* always
            comes from ``client``. Settlement lookup failures are logged
            and the spot fields are recorded as None.
    """

    def __init__(
        self,
        repo: _RepoLike,
        client: _ClientLike,
        settlements: dict[str, SettlementSource] | None = None,
    ) -> None:
        self._repo = repo
        self._client = client
        self._settlements: dict[str, SettlementSource] = settlements or {}

    def resolve_due_markets(self) -> int:
        """Resolve every market whose window closed and Polymarket has settled.

        Returns:
            The number of markets resolved this pass. Markets where
            Polymarket has not yet posted a winner are deferred for a
            later pass and not counted here.
        """
        now = datetime.now(timezone.utc)
        markets = self._repo.get_unresolved_markets_past_end(now)
        resolved = 0
        deferred = 0
        for m in markets:
            outcome = self._fetch_outcome(m)
            if outcome is None:
                deferred += 1
                continue

            start_spot, end_spot = self._diagnostic_spots(m)

            self._repo.update_market_resolution(
                m["id"],
                start_spot=start_spot,
                end_spot=end_spot,
                outcome=outcome,
                price_source_id=_PM_PRICE_SOURCE_ID,
            )
            for t in self._repo.get_trades_for_market(m["id"]):
                if t.get("pnl") is not None:
                    continue
                pnl = compute_pnl(
                    t["side"],
                    Decimal(str(t["fill_price_assumed"])),
                    outcome,
                    Decimal(str(t["size"])),
                )
                self._repo.update_trade_pnl(t["id"], pnl)
            resolved += 1
        if markets:
            # Always log when there's something past-end so the operator
            # can distinguish "nothing to resolve" from "resolver tried
            # but PM still pending". `pending_pm` typically clears within
            # 5-15 min of window close as UMA finalises settlement.
            logger.info(
                "resolver: past_end=%d resolved=%d pending_pm=%d",
                len(markets), resolved, deferred,
            )
        return resolved

    def _fetch_outcome(self, market: dict) -> Optional[str]:
        """Return ``YES`` / ``NO`` from Polymarket, or None to defer.

        Defers when Polymarket has not yet resolved the market, when the
        client call fails, or when the midpoint is in the open interval
        (which would indicate a paused or partially settled state we
        shouldn't act on).
        """
        pm_id = market.get("polymarket_id")
        if not pm_id:
            logger.warning("market %s has no polymarket_id; skipping", market.get("id"))
            return None
        try:
            state = self._client.fetch_market_state(pm_id)
        except Exception as exc:
            logger.warning("PM fetch failed for %s: %s", pm_id, exc)
            return None
        if state is None or not state.get("is_resolved"):
            return None
        midpoint = state.get("midpoint_price")
        if midpoint == Decimal("1"):
            return "YES"
        if midpoint == Decimal("0"):
            return "NO"
        logger.warning(
            "PM resolved %s with unexpected midpoint=%s; skipping",
            pm_id, midpoint,
        )
        return None

    def _diagnostic_spots(self, market: dict) -> tuple[Optional[Decimal], Optional[Decimal]]:
        """Best-effort start/end spot fetch for audit. Returns (None, None) on failure."""
        asset_id = market.get("asset_id") or "BTC"
        settlement = self._settlements.get(asset_id)
        if settlement is None:
            return None, None
        try:
            start_spot = settlement.price_at(market["window_start_ts"])
            end_spot = settlement.price_at(market["window_end_ts"])
        except Exception as exc:
            logger.warning(
                "settlement spot lookup failed for market %s: %s",
                market.get("polymarket_id"), exc,
            )
            return None, None
        return start_spot, end_spot

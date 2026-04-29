"""Registry-aware resolver for short-horizon binary markets.

Reads unresolved markets whose window has closed, fetches start/end spot
from the registered :class:`SettlementSource`, stamps the outcome plus a
``price_source_id`` audit field on the market row, and updates each linked
trade's realized P&L.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

from polyagent.services.quant.assets.sources.base import SettlementSource
from polyagent.services.quant.core.pnl import compute_pnl

logger = logging.getLogger("polyagent.services.quant.short_horizon.resolver")


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


class QuantResolver:
    """Resolve short-horizon markets and back-fill paper-trade P&L.

    Args:
        repo: Repository providing the read/update methods declared in
            :class:`_RepoLike`.
        settlements: Mapping from ``asset_id`` to its
            :class:`SettlementSource` for historical spot lookup.
    """

    def __init__(
        self,
        repo: _RepoLike,
        settlements: dict[str, SettlementSource],
    ) -> None:
        self._repo = repo
        self._settlements = settlements

    def resolve_due_markets(self) -> int:
        """Resolve every unresolved market whose window has already closed.

        Returns:
            The number of markets resolved this pass.
        """
        now = datetime.now(timezone.utc)
        markets = self._repo.get_unresolved_markets_past_end(now)
        resolved = 0
        for m in markets:
            asset_id = m.get("asset_id") or "BTC"
            settlement = self._settlements.get(asset_id)
            if settlement is None:
                logger.warning(
                    "no settlement source for asset_id=%s, skipping market %s",
                    asset_id, m.get("polymarket_id"),
                )
                continue
            start_spot = settlement.price_at(m["window_start_ts"])
            end_spot = settlement.price_at(m["window_end_ts"])
            if start_spot is None or end_spot is None:
                logger.info(
                    "skip resolution of %s: spot history unavailable",
                    m["polymarket_id"],
                )
                continue
            outcome = "YES" if end_spot >= start_spot else "NO"
            self._repo.update_market_resolution(
                m["id"],
                start_spot=start_spot,
                end_spot=end_spot,
                outcome=outcome,
                price_source_id=settlement.source_id(),
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
        if resolved:
            logger.info("resolved %d quant_short markets", resolved)
        return resolved

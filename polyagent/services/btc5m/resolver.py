"""Resolves BTC 5m markets whose window has ended and computes paper P&L."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

from polyagent.data.repositories.btc5m import Btc5mRepository
from polyagent.services.quant.core.pnl import compute_pnl

logger = logging.getLogger("polyagent.services.btc5m.resolver")


class SpotHistory(Protocol):
    """Anything that can answer 'what was the BTC spot at timestamp T?'."""

    def price_at(self, ts: datetime) -> Decimal | None: ...


class Btc5mResolver:
    """Resolves BTC 5m markets whose window has ended and records paper P&L."""

    def __init__(self, repo: Btc5mRepository, spot_history: SpotHistory) -> None:
        self._repo = repo
        self._history = spot_history

    def resolve_due_markets(self) -> int:
        """Resolve every market whose window_end_ts has passed.

        For each unresolved market past its window end:
        1. Fetch BTC spot at window_start_ts and window_end_ts.
        2. Determine outcome: YES if end_spot >= start_spot, else NO.
        3. Persist the resolution on the market row.
        4. Compute and persist P&L for any unresolved paper trades.

        Markets are skipped if spot history is unavailable for either endpoint.
        Trades that already have a non-None pnl are not overwritten.

        Returns:
            Number of markets successfully resolved.
        """
        now = datetime.now(timezone.utc)
        markets = self._repo.get_unresolved_markets_past_end(now)
        resolved = 0
        for m in markets:
            start_spot = self._history.price_at(m["window_start_ts"])
            end_spot = self._history.price_at(m["window_end_ts"])
            if start_spot is None or end_spot is None:
                logger.info(
                    "skip resolution of %s: spot history unavailable",
                    m["polymarket_id"],
                )
                continue

            outcome = "YES" if end_spot >= start_spot else "NO"
            self._repo.update_market_resolution(
                m["id"], start_spot=start_spot, end_spot=end_spot, outcome=outcome,
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
            logger.info("resolved %d btc5m markets", resolved)
        return resolved

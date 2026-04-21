"""Positions repository."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from polyagent.infra.database import Database
from polyagent.models import ExitReason, PositionStatus

logger = logging.getLogger("polyagent.repositories.positions")

INSERT_POSITION = """
    INSERT INTO positions (
        id, thesis_id, market_id, side, entry_price, target_price,
        kelly_fraction, position_size, current_price, status,
        paper_trade, opened_at, volume_at_entry
    ) VALUES (
        %(id)s, %(thesis_id)s, %(market_id)s, %(side)s,
        %(entry_price)s, %(target_price)s, %(kelly_fraction)s,
        %(position_size)s, %(current_price)s, %(status)s,
        %(paper_trade)s, %(opened_at)s, %(volume_at_entry)s
    )
"""

SELECT_OPEN = """
    SELECT p.*, m.polymarket_id, m.question, m.token_id
    FROM positions p
    JOIN markets m ON p.market_id = m.id
    WHERE p.status = 'open'
    ORDER BY p.opened_at DESC
"""

SELECT_CLOSED = """
    SELECT p.*, m.polymarket_id, m.question
    FROM positions p
    JOIN markets m ON p.market_id = m.id
    WHERE p.status = 'closed'
    ORDER BY p.closed_at DESC
    LIMIT %(limit)s
"""

CLOSE_POSITION = """
    UPDATE positions
    SET status = 'closed', exit_reason = %(exit_reason)s,
        pnl = %(pnl)s, current_price = %(current_price)s,
        closed_at = %(closed_at)s
    WHERE id = %(id)s
"""

UPDATE_CURRENT_PRICE = """
    UPDATE positions SET current_price = %(current_price)s WHERE id = %(id)s
"""

SELECT_CAPITAL_STATE = """
    SELECT
        COALESCE(SUM(position_size) FILTER (WHERE status = 'open'), 0) AS open_capital,
        COALESCE(SUM(pnl) FILTER (WHERE status = 'closed'), 0) AS realized_pnl
    FROM positions
"""


class PositionRepository:
    """CRUD operations for the positions table."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def insert(self, position) -> None:
        """Insert a new position."""
        with self._db.cursor() as cur:
            cur.execute(
                INSERT_POSITION,
                {
                    "id": position.id,
                    "thesis_id": position.thesis_id,
                    "market_id": position.market_id,
                    "side": position.side.value,
                    "entry_price": position.entry_price,
                    "target_price": position.target_price,
                    "kelly_fraction": position.kelly_fraction,
                    "position_size": position.position_size,
                    "current_price": position.current_price,
                    "status": position.status.value,
                    "paper_trade": position.paper_trade,
                    "opened_at": position.opened_at,
                    "volume_at_entry": position.volume_at_entry,
                },
            )

    def get_open(self) -> list[dict]:
        """Get all open positions with market info."""
        with self._db.cursor() as cur:
            cur.execute(SELECT_OPEN)
            return cur.fetchall()

    def get_closed(self, limit: int = 50) -> list[dict]:
        """Get closed positions."""
        with self._db.cursor() as cur:
            cur.execute(SELECT_CLOSED, {"limit": limit})
            return cur.fetchall()

    def close(
        self,
        position_id: UUID,
        exit_reason: ExitReason,
        pnl: Decimal,
        current_price: Decimal,
    ) -> None:
        """Close a position."""
        with self._db.cursor() as cur:
            cur.execute(
                CLOSE_POSITION,
                {
                    "id": position_id,
                    "exit_reason": exit_reason.value,
                    "pnl": pnl,
                    "current_price": current_price,
                    "closed_at": datetime.now(timezone.utc),
                },
            )

    def update_price(self, position_id: UUID, current_price: Decimal) -> None:
        """Update a position's current price."""
        with self._db.cursor() as cur:
            cur.execute(UPDATE_CURRENT_PRICE, {"id": position_id, "current_price": current_price})

    def get_capital_state(self) -> tuple[Decimal, Decimal]:
        """Return (open_capital, realized_pnl) summed over the positions table.

        open_capital = sum(position_size) over open positions — capital currently tied up.
        realized_pnl = sum(pnl) over closed positions — cumulative gains/losses.
        """
        with self._db.cursor() as cur:
            cur.execute(SELECT_CAPITAL_STATE)
            row = cur.fetchone()
            if row is None:
                return Decimal("0"), Decimal("0")
            return Decimal(str(row["open_capital"])), Decimal(str(row["realized_pnl"]))

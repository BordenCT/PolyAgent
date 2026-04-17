"""Trade log repository — captures raw request/response for every order action."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from polyagent.infra.database import Database

logger = logging.getLogger("polyagent.repositories.trade_log")

INSERT_LOG = """
    INSERT INTO trade_log (
        position_id, action, reason, raw_request, raw_response, error, logged_at
    ) VALUES (
        %(position_id)s, %(action)s, %(reason)s, %(raw_request)s,
        %(raw_response)s, %(error)s, %(logged_at)s
    )
"""

SELECT_BY_POSITION = """
    SELECT * FROM trade_log
    WHERE position_id = %(position_id)s
    ORDER BY logged_at
"""


class TradeLogRepository:
    """Append-only log of raw order-placement requests and responses."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def insert(
        self,
        position_id: UUID,
        action: str,
        reason: str | None = None,
        raw_request: dict[str, Any] | None = None,
        raw_response: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Record a trade action against a position.

        Args:
            position_id: The position this action relates to.
            action: Short action label ("OPEN_PAPER", "OPEN_LIVE", "CLOSE_PAPER", etc.).
            reason: Optional human-readable reason (exit reason, skip cause).
            raw_request: The request payload sent to the broker, if any.
            raw_response: The raw response received, if any.
            error: Error message if the action failed.
        """
        with self._db.cursor() as cur:
            cur.execute(
                INSERT_LOG,
                {
                    "position_id": position_id,
                    "action": action,
                    "reason": reason,
                    "raw_request": json.dumps(raw_request) if raw_request is not None else None,
                    "raw_response": json.dumps(raw_response) if raw_response is not None else None,
                    "error": error,
                    "logged_at": datetime.now(timezone.utc),
                },
            )

    def get_by_position(self, position_id: UUID) -> list[dict]:
        """Return all log rows for a position, ordered oldest-first."""
        with self._db.cursor() as cur:
            cur.execute(SELECT_BY_POSITION, {"position_id": position_id})
            return cur.fetchall()

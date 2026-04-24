"""Market data repository."""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

from polyagent.infra.database import Database
from polyagent.models import MarketClass, MarketData, MarketStatus, Score

logger = logging.getLogger("polyagent.repositories.markets")

UPSERT_MARKET = """
    INSERT INTO markets (
        polymarket_id, question, category, token_id,
        midpoint_price, bids_depth, asks_depth,
        hours_to_resolution, volume_24h, status, market_class
    ) VALUES (
        %(polymarket_id)s, %(question)s, %(category)s, %(token_id)s,
        %(midpoint_price)s, %(bids_depth)s, %(asks_depth)s,
        %(hours_to_resolution)s, %(volume_24h)s, %(status)s, %(market_class)s
    )
    ON CONFLICT (polymarket_id) DO UPDATE SET
        midpoint_price = EXCLUDED.midpoint_price,
        bids_depth = EXCLUDED.bids_depth,
        asks_depth = EXCLUDED.asks_depth,
        hours_to_resolution = EXCLUDED.hours_to_resolution,
        volume_24h = EXCLUDED.volume_24h,
        market_class = EXCLUDED.market_class,
        scanned_at = NOW()
    RETURNING id
"""

SELECT_BY_STATUS = """
    SELECT id, polymarket_id, question, category, token_id,
           midpoint_price, bids_depth, asks_depth,
           hours_to_resolution, volume_24h, status
    FROM markets
    WHERE status = %(status)s
    ORDER BY scanned_at DESC
"""

UPDATE_STATUS = """
    UPDATE markets SET status = %(status)s WHERE id = %(id)s
"""

UPDATE_SCORE = """
    UPDATE markets SET score = %(score)s, status = %(status)s WHERE id = %(id)s
"""


class MarketRepository:
    """CRUD operations for the markets table."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def upsert(self, market: MarketData, status: MarketStatus = MarketStatus.QUEUED) -> UUID:
        """Insert or update a market, return its UUID.

        Args:
            market: The market data to persist.
            status: Initial status for new markets (default: QUEUED).

        Returns:
            The UUID of the upserted market row.
        """
        market_class = (market.market_class or MarketClass.OTHER).value
        with self._db.cursor() as cur:
            cur.execute(
                UPSERT_MARKET,
                {
                    "polymarket_id": market.polymarket_id,
                    "question": market.question,
                    "category": market.category,
                    "token_id": market.token_id,
                    "midpoint_price": market.midpoint_price,
                    "bids_depth": market.bids_depth,
                    "asks_depth": market.asks_depth,
                    "hours_to_resolution": market.hours_to_resolution,
                    "volume_24h": market.volume_24h,
                    "status": status.value,
                    "market_class": market_class,
                },
            )
            row = cur.fetchone()
            return row["id"]

    def get_by_status(self, status: MarketStatus) -> list[dict]:
        """Fetch all markets with a given status.

        Args:
            status: The market status to filter by.

        Returns:
            List of market row dicts ordered by scanned_at descending.
        """
        with self._db.cursor() as cur:
            cur.execute(SELECT_BY_STATUS, {"status": status.value})
            return cur.fetchall()

    def update_status(self, market_id: UUID, status: MarketStatus) -> None:
        """Update a market's status.

        Args:
            market_id: UUID of the market to update.
            status: The new status value.
        """
        with self._db.cursor() as cur:
            cur.execute(UPDATE_STATUS, {"id": market_id, "status": status.value})

    def update_score(self, market_id: UUID, score: Score, status: MarketStatus) -> None:
        """Update a market's score and status.

        Args:
            market_id: UUID of the market to update.
            score: The computed Score to persist as JSON.
            status: The new status value.
        """
        score_json = json.dumps(
            {"gap": score.gap, "depth": score.depth, "hours": score.hours, "ev": score.ev}
        )
        with self._db.cursor() as cur:
            cur.execute(UPDATE_SCORE, {"id": market_id, "score": score_json, "status": status.value})

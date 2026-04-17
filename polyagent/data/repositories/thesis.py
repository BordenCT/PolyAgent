"""Thesis repository."""
from __future__ import annotations

import json
import logging
from uuid import UUID

from polyagent.infra.database import Database
from polyagent.models import Consensus, ThesisChecks

logger = logging.getLogger("polyagent.repositories.thesis")

INSERT_THESIS = """
    INSERT INTO thesis (
        id, market_id, claude_estimate, confidence,
        checks, checks_passed, thesis_text,
        strategy_votes, consensus
    ) VALUES (
        %(id)s, %(market_id)s, %(claude_estimate)s, %(confidence)s,
        %(checks)s, %(checks_passed)s, %(thesis_text)s,
        %(strategy_votes)s, %(consensus)s
    )
"""

SELECT_BY_MARKET = """
    SELECT * FROM thesis WHERE market_id = %(market_id)s
    ORDER BY created_at DESC LIMIT 1
"""

UPDATE_VOTES = """
    UPDATE thesis
    SET strategy_votes = %(strategy_votes)s, consensus = %(consensus)s
    WHERE id = %(id)s
"""


class ThesisRepository:
    """CRUD operations for the thesis table."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def insert(self, thesis) -> None:
        """Insert a new thesis."""
        with self._db.cursor() as cur:
            cur.execute(
                INSERT_THESIS,
                {
                    "id": thesis.id,
                    "market_id": thesis.market_id,
                    "claude_estimate": thesis.claude_estimate,
                    "confidence": thesis.confidence,
                    "checks": json.dumps({
                        "base_rate": thesis.checks.base_rate,
                        "news": thesis.checks.news,
                        "whale": thesis.checks.whale,
                        "disposition": thesis.checks.disposition,
                    }),
                    "checks_passed": thesis.checks.passed_count,
                    "thesis_text": thesis.thesis_text,
                    "strategy_votes": json.dumps(
                        {k: v.value if hasattr(v, "value") else v
                         for k, v in thesis.strategy_votes.items()}
                    ),
                    "consensus": thesis.consensus.value,
                },
            )

    def get_by_market(self, market_id: UUID) -> dict | None:
        """Get the latest thesis for a market."""
        with self._db.cursor() as cur:
            cur.execute(SELECT_BY_MARKET, {"market_id": market_id})
            return cur.fetchone()

    def update_votes(self, thesis_id: UUID, votes: dict, consensus: Consensus) -> None:
        """Update strategy votes and consensus on a thesis."""
        with self._db.cursor() as cur:
            cur.execute(
                UPDATE_VOTES,
                {
                    "id": thesis_id,
                    "strategy_votes": json.dumps(votes),
                    "consensus": consensus.value,
                },
            )

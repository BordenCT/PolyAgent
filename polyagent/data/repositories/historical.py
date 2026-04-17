"""Historical outcomes repository with pgvector RAG support."""
from __future__ import annotations

import logging
from uuid import UUID

from polyagent.infra.database import Database

logger = logging.getLogger("polyagent.repositories.historical")

FIND_SIMILAR = """
    SELECT polymarket_id, question, outcome, final_price, resolution_date, metadata,
           1 - (question_embedding <=> %(embedding)s::vector) AS similarity
    FROM historical_outcomes
    WHERE question_embedding IS NOT NULL
    ORDER BY question_embedding <=> %(embedding)s::vector
    LIMIT %(limit)s
"""

INSERT_OUTCOME = """
    INSERT INTO historical_outcomes (
        polymarket_id, question, question_embedding, outcome,
        final_price, resolution_date, metadata
    ) VALUES (
        %(polymarket_id)s, %(question)s, %(embedding)s::vector,
        %(outcome)s, %(final_price)s, %(resolution_date)s, %(metadata)s
    )
"""


class HistoricalRepository:
    """Historical market outcomes with vector similarity search."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def find_similar(
        self, embedding: list[float], limit: int = 10
    ) -> list[dict]:
        """Find similar historical outcomes by embedding similarity."""
        embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
        with self._db.cursor() as cur:
            cur.execute(FIND_SIMILAR, {"embedding": embedding_str, "limit": limit})
            return cur.fetchall()

    def insert(self, outcome: dict, embedding: list[float] | None = None) -> None:
        """Insert a historical outcome."""
        embedding_str = (
            "[" + ",".join(str(x) for x in embedding) + "]" if embedding else None
        )
        with self._db.cursor() as cur:
            cur.execute(
                INSERT_OUTCOME,
                {
                    "polymarket_id": outcome["polymarket_id"],
                    "question": outcome["question"],
                    "embedding": embedding_str,
                    "outcome": outcome.get("outcome"),
                    "final_price": outcome.get("final_price"),
                    "resolution_date": outcome.get("resolution_date"),
                    "metadata": "{}",
                },
            )

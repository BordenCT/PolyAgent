"""One-shot backfill for markets.market_class.

Reads every row from `markets`, applies the classifier, and updates the
market_class column. Idempotent — safe to re-run after classifier tweaks.
Commits per row so a transient error on one row does not roll back work
already persisted.

Usage:
    python -m polyagent.scripts.backfill_market_class
"""
from __future__ import annotations

import logging
import sys
from collections import Counter

from polyagent.infra.config import Settings
from polyagent.infra.database import Database
from polyagent.infra.logging import setup_logging
from polyagent.services.classifier import classify

logger = logging.getLogger("polyagent.scripts.backfill_market_class")


def backfill(db: Database) -> Counter[str]:
    """Classify every market row and persist the result.

    Fetches all markets in a single read, then updates each row in its own
    cursor context (one commit per row). Safe to re-run; later runs simply
    overwrite with the same or updated classification.

    Args:
        db: An open Database whose connection pool is ready to use.

    Returns:
        A Counter keyed by MarketClass value (e.g. {"sports": 18, ...})
        reflecting the classes assigned during this run.
    """
    counts: Counter[str] = Counter()

    with db.cursor() as cur:
        cur.execute("SELECT id, question, category FROM markets")
        rows = cur.fetchall()

    for row in rows:
        try:
            cls = classify(row["question"] or "", row["category"] or "")
        except Exception:
            logger.exception("classify failed for market %s", row["id"])
            continue

        try:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE markets SET market_class = %s WHERE id = %s",
                    (cls.value, row["id"]),
                )
        except Exception:
            logger.exception("UPDATE failed for market %s", row["id"])
            continue

        counts[cls.value] += 1

    logger.info("backfill complete: %s", dict(counts))
    return counts


def main() -> int:
    """Entry point: open DB from environment, run backfill, close cleanly."""
    setup_logging()
    settings = Settings.from_env()
    db = Database(settings)
    try:
        backfill(db)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

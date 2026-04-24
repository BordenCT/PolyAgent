"""End-to-end test for the market_class backfill script."""
from __future__ import annotations

from uuid import uuid4

import pytest

from polyagent.infra.config import Settings
from polyagent.infra.database import Database
from polyagent.scripts.backfill_market_class import backfill


pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_db(settings: Settings):
    db = Database(settings)
    with db.cursor() as cur:
        cur.execute("DELETE FROM trade_log")
        cur.execute("DELETE FROM positions")
        cur.execute("DELETE FROM thesis")
        cur.execute("DELETE FROM markets")

        # All rows inserted with the default market_class ('other').
        cur.execute(
            """
            INSERT INTO markets (polymarket_id, question, category, token_id,
                                 midpoint_price, bids_depth, asks_depth,
                                 hours_to_resolution, volume_24h, status)
            VALUES
                ('0x1', 'Madrid Open: A vs B', 'Sports',   't1', 0.5, 100, 100, 24, 0, 'queued'),
                ('0x2', 'Will BTC hit $80,000?', 'Crypto',  't2', 0.5, 100, 100, 24, 0, 'queued'),
                ('0x3', 'Will Trump win re-election?', 'Politics', 't3', 0.5, 100, 100, 24, 0, 'queued'),
                ('0x4', 'Will CPI YoY be above 3%?', 'Economics', 't4', 0.5, 100, 100, 24, 0, 'queued'),
                ('0x5', 'Will SpaceX launch by Q3?', 'Tech',  't5', 0.5, 100, 100, 24, 0, 'queued')
            """
        )
    yield db
    db.close()


def test_backfill_assigns_correct_class(seeded_db):
    counts = backfill(seeded_db)
    assert counts["sports"] == 1
    assert counts["crypto"] == 1
    assert counts["politics"] == 1
    assert counts["macro"] == 1
    assert counts["other"] == 1

    with seeded_db.cursor() as cur:
        cur.execute(
            "SELECT polymarket_id, market_class::text FROM markets ORDER BY polymarket_id"
        )
        rows = cur.fetchall()

    result = {r["polymarket_id"]: r["market_class"] for r in rows}
    assert result == {
        "0x1": "sports",
        "0x2": "crypto",
        "0x3": "politics",
        "0x4": "macro",
        "0x5": "other",
    }


def test_backfill_is_idempotent(seeded_db):
    backfill(seeded_db)
    counts2 = backfill(seeded_db)
    assert counts2["sports"] == 1
    assert counts2["crypto"] == 1
    assert counts2["politics"] == 1
    assert counts2["macro"] == 1
    assert counts2["other"] == 1

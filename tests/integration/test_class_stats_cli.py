"""End-to-end test for `polyagent class-stats` against a real DB.

Requires --run-integration and a running polyagent_test database matching
the schema in db/migrations/. This test seeds markets+positions directly,
invokes the CLI, and asserts aggregates in the rendered output.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from click.testing import CliRunner

from polyagent.cli.class_stats import class_stats
from polyagent.infra.config import Settings
from polyagent.infra.database import Database


pytestmark = pytest.mark.integration

_TEST_DB_URL = "postgresql://polyagent:polyagent@localhost:5432/polyagent_test"


@pytest.fixture
def seeded_db(settings: Settings):
    db = Database(settings)
    with db.cursor() as cur:
        cur.execute("DELETE FROM trade_log")
        cur.execute("DELETE FROM positions")
        cur.execute("DELETE FROM thesis")
        cur.execute("DELETE FROM markets")

        sports_id = uuid4()
        crypto_id = uuid4()
        cur.execute(
            """
            INSERT INTO markets (id, polymarket_id, question, category, token_id,
                                 midpoint_price, bids_depth, asks_depth,
                                 hours_to_resolution, volume_24h, status, market_class)
            VALUES
                (%s, '0xs', 'Team A vs. Team B', 'Sports', 't1', 0.5, 100, 100, 24, 0, 'traded', 'sports'),
                (%s, '0xc', 'Will BTC hit $80,000?', 'Crypto', 't2', 0.5, 100, 100, 24, 0, 'traded', 'crypto')
            """,
            (sports_id, crypto_id),
        )

        thesis_s = uuid4()
        thesis_c = uuid4()
        cur.execute(
            """
            INSERT INTO thesis (id, market_id, claude_estimate, confidence, checks,
                                checks_passed, thesis_text, strategy_votes, consensus)
            VALUES
                (%s, %s, 0.5, 0.8, '{}'::jsonb, 0, '', '{}'::jsonb, 'full'),
                (%s, %s, 0.5, 0.8, '{}'::jsonb, 0, '', '{}'::jsonb, 'full')
            """,
            (thesis_s, sports_id, thesis_c, crypto_id),
        )

        opened = datetime.now(timezone.utc) - timedelta(hours=10)
        closed = datetime.now(timezone.utc)
        cur.execute(
            """
            INSERT INTO positions (id, thesis_id, market_id, side, entry_price,
                                   target_price, kelly_fraction, position_size,
                                   current_price, status, exit_reason, pnl,
                                   paper_trade, opened_at, closed_at)
            VALUES
                (%s, %s, %s, 'BUY', 0.3, 0.7, 0.1, 10.0, 0.0, 'closed', 'RESOLVED_NO', -1.00, true, %s, %s),
                (%s, %s, %s, 'SELL', 0.5, 0.1, 0.1, 10.0, 0.0, 'closed', 'RESOLVED_NO',  0.50, true, %s, %s),
                (%s, %s, %s, 'BUY', 0.2, 0.6, 0.1, 10.0, 0.0, 'closed', 'RESOLVED_NO', -0.25, true, %s, %s)
            """,
            (
                uuid4(), thesis_s, sports_id, opened, closed,
                uuid4(), thesis_s, sports_id, opened, closed,
                uuid4(), thesis_c, crypto_id, opened, closed,
            ),
        )
    yield db
    db.close()


def test_class_stats_reports_per_class_aggregates(seeded_db):
    runner = CliRunner()
    # Pass the test DB URL explicitly so the CLI's own Settings.from_env() picks it up.
    result = runner.invoke(
        class_stats,
        [],
        env={
            "DATABASE_URL": _TEST_DB_URL,
            "ANTHROPIC_API_KEY": "sk-test-key",
            "PAPER_TRADE": "true",
        },
    )
    assert result.exit_code == 0, result.output
    # Sports: 2 trades, 1 win, 1 loss, net = -1.00 + 0.50 = -0.50
    assert "sports" in result.output
    assert "2" in result.output  # sports trades
    # Crypto: 1 trade, 0 wins, 1 loss, net = -0.25
    assert "crypto" in result.output
    # Totals row
    assert "TOTAL" in result.output or "Total" in result.output

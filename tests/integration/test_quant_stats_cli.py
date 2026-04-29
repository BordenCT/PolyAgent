"""End-to-end test for `polyagent quant-stats` against a real DB."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from click.testing import CliRunner

from polyagent.cli.quant_stats import quant_stats
from polyagent.infra.config import Settings
from polyagent.infra.database import Database

pytestmark = pytest.mark.integration

_TEST_DB_URL = "postgresql://polyagent:polyagent@localhost:5432/polyagent_test"


@pytest.fixture
def seeded_db(settings: Settings):
    db = Database(settings)
    with db.cursor() as cur:
        cur.execute("DELETE FROM quant_short_trades")
        cur.execute("DELETE FROM quant_short_markets")

        mid = uuid4()
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=10)
        end = now - timedelta(minutes=5)
        cur.execute(
            """
            INSERT INTO quant_short_markets (id, polymarket_id, slug, token_id_yes, token_id_no,
                                             window_duration_s, window_start_ts, window_end_ts,
                                             asset_id, start_spot, end_spot, outcome, resolved_at)
            VALUES (%s, '0x1', 'btc-updown-5m-1234567890', 'y', 'n',
                    300, %s, %s, 'BTC', 65000, 65100, 'YES', NOW())
            """, (mid, start, end),
        )
        cur.execute(
            """
            INSERT INTO quant_short_trades (market_id, side, fill_price_assumed, size,
                                            estimator_p_up, spot_at_decision,
                                            vol_at_decision, edge_at_decision, pnl, resolved_at)
            VALUES
                (%s, 'YES', 0.40, 5.00, 0.55, 65000, 0.40,  0.10,  3.00, NOW()),
                (%s, 'YES', 0.60, 5.00, 0.70, 65000, 0.40,  0.08,  2.00, NOW()),
                (%s, 'NO',  0.30, 5.00, 0.40, 65000, 0.40, -0.10, -1.50, NOW())
            """, (mid, mid, mid),
        )
    yield db
    db.close()


def test_quant_stats_summary(seeded_db):
    runner = CliRunner()
    result = runner.invoke(quant_stats, [], env={"DATABASE_URL": _TEST_DB_URL})
    assert result.exit_code == 0, result.output
    assert "3" in result.output
    assert "2/1" in result.output or "2" in result.output
    # Total PnL = 3 + 2 - 1.5 = 3.50
    assert "3.50" in result.output or "+$3.50" in result.output


def test_quant_stats_filters_by_asset(seeded_db):
    runner = CliRunner()
    result = runner.invoke(quant_stats, ["--asset", "BTC"], env={"DATABASE_URL": _TEST_DB_URL})
    assert result.exit_code == 0, result.output
    assert "3" in result.output
    # ETH filter should match nothing for the BTC-only seeded data.
    result_eth = runner.invoke(quant_stats, ["--asset", "ETH"], env={"DATABASE_URL": _TEST_DB_URL})
    assert result_eth.exit_code == 0, result_eth.output
    assert "Trades" in result_eth.output

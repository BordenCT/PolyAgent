"""End-to-end pipeline test in paper trading mode.

Requires: podman-compose up polyagent-db
Run with: pytest tests/integration/ -v -m integration
"""
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from polyagent.data.repositories.markets import MarketRepository
from polyagent.data.repositories.positions import PositionRepository
from polyagent.data.repositories.thesis import ThesisRepository
from polyagent.infra.config import Settings
from polyagent.infra.database import Database
from polyagent.models import (
    Consensus,
    ExitReason,
    MarketData,
    MarketStatus,
    PositionSide,
    ThesisChecks,
    Thesis,
    Vote,
    VoteAction,
)
from polyagent.services.executor import ExecutorService
from polyagent.services.exit_monitor import ExitMonitorService
from polyagent.services.scanner import ScannerService


@pytest.fixture
def db():
    """Connect to test database."""
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test")
    os.environ.setdefault(
        "DATABASE_URL",
        "postgresql://polyagent:polyagent@localhost:5432/polyagent",
    )
    settings = Settings.from_env()
    database = Database(settings)
    yield database
    database.close()


@pytest.mark.integration
class TestFullPipeline:
    """Tests the full scan -> evaluate -> execute -> exit pipeline."""

    def test_scanner_filters_markets(self):
        """Scanner correctly identifies high-EV markets."""
        scanner = ScannerService(min_gap=0.07, min_depth=500, min_hours=4, max_hours=168)

        good_market = MarketData(
            polymarket_id="0xgood",
            question="Will BTC hit 150k by July?",
            category="crypto",
            token_id="tok_good",
            midpoint_price=Decimal("0.40"),
            bids_depth=Decimal("2000"),
            asks_depth=Decimal("1800"),
            hours_to_resolution=48.0,
            volume_24h=Decimal("150000"),
        )
        bad_market = MarketData(
            polymarket_id="0xbad",
            question="Will it rain tomorrow?",
            category="weather",
            token_id="tok_bad",
            midpoint_price=Decimal("0.50"),
            bids_depth=Decimal("100"),
            asks_depth=Decimal("50"),
            hours_to_resolution=2.0,
            volume_24h=Decimal("500"),
        )

        survivors = scanner.scan_batch(
            [good_market, bad_market],
            {"0xgood": 0.55, "0xbad": 0.52},
        )
        assert len(survivors) == 1
        assert survivors[0][0].polymarket_id == "0xgood"

    def test_executor_full_pipeline(self):
        """Executor correctly sizes and creates paper positions."""
        executor = ExecutorService(kelly_max_fraction=0.25, bankroll=800, paper_trade=True)

        thesis = Thesis.create(
            market_id=uuid4(),
            claude_estimate=0.82,
            confidence=0.85,
            checks=ThesisChecks(base_rate=True, news=True, whale=False, disposition=True),
            thesis_text="Strong crypto momentum with favorable base rate",
        )

        votes = [
            Vote(action=VoteAction.BUY, confidence=0.8, reason="Arbitrage gap detected"),
            Vote(action=VoteAction.BUY, confidence=0.7, reason="Convergence signal"),
            Vote(action=VoteAction.HOLD, confidence=0.3, reason="No whale data"),
        ]

        position = executor.execute(thesis=thesis, votes=votes, market_price=Decimal("0.65"))

        assert position is not None
        assert position.paper_trade is True
        assert position.side == PositionSide.BUY
        assert float(position.position_size) > 0
        assert position.kelly_fraction > 0

    def test_exit_monitor_lifecycle(self):
        """Exit monitor correctly triggers all 3 exit types."""
        monitor = ExitMonitorService(target_pct=0.85, volume_multiplier=3, stale_hours=24, stale_threshold=0.02)

        # 1. No exit — healthy position
        assert monitor.check_exit(
            entry_price=Decimal("0.40"), target_price=Decimal("0.57"),
            current_price=Decimal("0.45"), volume_10min=100, avg_volume_10min=100, hours_since_entry=6,
        ) is None

        # 2. Target hit
        assert monitor.check_exit(
            entry_price=Decimal("0.40"), target_price=Decimal("0.57"),
            current_price=Decimal("0.55"), volume_10min=100, avg_volume_10min=100, hours_since_entry=6,
        ) == ExitReason.TARGET_HIT

        # 3. Volume spike
        assert monitor.check_exit(
            entry_price=Decimal("0.40"), target_price=Decimal("0.57"),
            current_price=Decimal("0.42"), volume_10min=400, avg_volume_10min=100, hours_since_entry=6,
        ) == ExitReason.VOLUME_EXIT

        # 4. Stale thesis — price barely moved (< 2% threshold)
        assert monitor.check_exit(
            entry_price=Decimal("0.40"), target_price=Decimal("0.57"),
            current_price=Decimal("0.405"), volume_10min=100, avg_volume_10min=100, hours_since_entry=30,
        ) == ExitReason.STALE_THESIS

    def test_database_round_trip(self, db):
        """Market -> thesis -> position persists correctly through DB."""
        market_repo = MarketRepository(db)
        thesis_repo = ThesisRepository(db)
        position_repo = PositionRepository(db)

        # Insert market
        market = MarketData(
            polymarket_id=f"0xtest_{uuid4().hex[:8]}",
            question="Integration test market",
            category="test",
            token_id="tok_test",
            midpoint_price=Decimal("0.50"),
            bids_depth=Decimal("1000"),
            asks_depth=Decimal("900"),
            hours_to_resolution=24.0,
            volume_24h=Decimal("50000"),
        )
        market_id = market_repo.upsert(market)

        # Insert thesis
        thesis = Thesis.create(
            market_id=market_id,
            claude_estimate=0.75,
            confidence=0.80,
            checks=ThesisChecks(base_rate=True, news=True, whale=False, disposition=True),
            thesis_text="Test thesis",
        )
        thesis_repo.insert(thesis)

        # Verify thesis retrieval
        stored = thesis_repo.get_by_market(market_id)
        assert stored is not None
        assert float(stored["claude_estimate"]) == 0.75

        # Insert position
        from polyagent.models import Position
        position = Position.open_paper(
            thesis_id=thesis.id,
            market_id=market_id,
            side=PositionSide.BUY,
            entry_price=Decimal("0.50"),
            target_price=Decimal("0.70"),
            kelly_fraction=0.12,
            position_size=Decimal("96.00"),
        )
        position_repo.insert(position)

        # Verify position retrieval
        open_positions = position_repo.get_open()
        found = [p for p in open_positions if p["id"] == position.id]
        assert len(found) == 1
        assert found[0]["paper_trade"] is True

        # Close position
        position_repo.close(
            position.id,
            ExitReason.TARGET_HIT,
            Decimal("14.40"),
            Decimal("0.575"),
        )

        closed = position_repo.get_closed(limit=10)
        found_closed = [p for p in closed if p["id"] == position.id]
        assert len(found_closed) == 1
        assert found_closed[0]["exit_reason"] == "TARGET_HIT"
        assert float(found_closed[0]["pnl"]) == 14.40

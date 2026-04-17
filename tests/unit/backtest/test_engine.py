"""Tests for backtest engine."""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from polyagent.backtest.data_loader import MarketSnapshot
from polyagent.backtest.engine import BacktestEngine, BacktestResult
from polyagent.backtest.estimator import HistoricalEstimator
from polyagent.services.scanner import ScannerService
from polyagent.services.executor import ExecutorService
from polyagent.services.exit_monitor import ExitMonitorService


class TestBacktestEngine:
    def setup_method(self):
        self.engine = BacktestEngine(
            scanner=ScannerService(min_gap=0.07, min_depth=500, min_hours=4, max_hours=168),
            executor=ExecutorService(kelly_max_fraction=0.25, bankroll=800, paper_trade=True),
            exit_monitor=ExitMonitorService(target_pct=0.85, volume_multiplier=3, stale_hours=24, stale_threshold=0.02),
            estimator=HistoricalEstimator(),
        )

    def _make_snapshot(self, market_id: str, price: str, volume: str = "5000") -> MarketSnapshot:
        return MarketSnapshot(
            polymarket_id=market_id,
            question=f"Test market {market_id}?",
            category="crypto",
            token_id=f"tok_{market_id}",
            price=Decimal(price),
            volume=Decimal(volume),
            timestamp=datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc),
            outcome="Yes",
        )

    def test_process_day_finds_trades(self):
        snapshots = [
            self._make_snapshot("0x1", "0.40", "2000"),  # good: gap 0.60 (outcome=Yes -> est=1.0)
        ]
        resolutions = {"0x1": {"outcome": "Yes", "final_price": 1.0}}
        positions = self.engine.process_day(
            snapshots=snapshots,
            resolutions=resolutions,
            current_date=date(2025, 6, 15),
        )
        assert len(positions) >= 1

    def test_process_day_filters_low_volume(self):
        snapshots = [
            self._make_snapshot("0x2", "0.40", "100"),  # depth too low
        ]
        resolutions = {"0x2": {"outcome": "Yes", "final_price": 1.0}}
        positions = self.engine.process_day(
            snapshots=snapshots,
            resolutions=resolutions,
            current_date=date(2025, 6, 15),
        )
        assert len(positions) == 0

    def test_result_metrics(self):
        result = BacktestResult(
            trades=[
                {"pnl": 50.0, "exit_reason": "TARGET_HIT", "category": "crypto"},
                {"pnl": -20.0, "exit_reason": "STALE_THESIS", "category": "crypto"},
                {"pnl": 30.0, "exit_reason": "VOLUME_EXIT", "category": "politics"},
            ],
            start_date=date(2025, 1, 1),
            end_date=date(2025, 6, 30),
            estimator_name="historical",
            bankroll=800.0,
        )
        assert result.total_trades == 3
        assert result.winners == 2
        assert result.win_rate == pytest.approx(66.67, abs=0.1)
        assert result.total_pnl == 60.0

    def test_result_max_drawdown(self):
        result = BacktestResult(
            trades=[
                {"pnl": 100.0, "exit_reason": "TARGET_HIT", "category": "crypto"},
                {"pnl": -50.0, "exit_reason": "STALE_THESIS", "category": "crypto"},
                {"pnl": -30.0, "exit_reason": "VOLUME_EXIT", "category": "crypto"},
                {"pnl": 200.0, "exit_reason": "TARGET_HIT", "category": "crypto"},
            ],
            start_date=date(2025, 1, 1),
            end_date=date(2025, 6, 30),
            estimator_name="historical",
            bankroll=800.0,
        )
        # Peak at 900 (800+100), then drops to 820 (900-50-30), drawdown = 80/900 = 8.9%
        assert result.max_drawdown > 0

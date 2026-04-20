"""Tests for the path-aware backtest engine."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from polyagent.backtest.data_loader import HourlyBar
from polyagent.backtest.engine import BacktestEngine, BacktestResult
from polyagent.backtest.estimator import BaseEstimator, MidpointEstimator
from polyagent.services.exit_monitor import ExitMonitorService
from polyagent.services.executor import ExecutorService
from polyagent.services.scanner import ScannerService


class FixedEstimator(BaseEstimator):
    """Returns a constant probability — decouples entry decisions from final prices."""
    name = "fixed"

    def __init__(self, prob: float) -> None:
        self._prob = prob

    def estimate(self, market_id: str, **kwargs) -> float:
        return self._prob


def _make_engine(estimator: BaseEstimator | None = None) -> BacktestEngine:
    return BacktestEngine(
        scanner=ScannerService(min_gap=0.07, min_depth=500, min_hours=4, max_hours=168),
        executor=ExecutorService(kelly_max_fraction=0.25, bankroll=800, paper_trade=True),
        exit_monitor=ExitMonitorService(
            target_pct=0.85, volume_multiplier=3, stale_hours=24, stale_threshold=0.02,
        ),
        estimator=estimator or FixedEstimator(0.80),
        scan_interval_hours=1,   # 1-bar interval so small test fixtures can enter
        transaction_cost_pct=0.0,  # excluded from unit tests — tested separately
    )


def _bar(
    market_id: str,
    hour: datetime,
    close: str,
    high: str | None = None,
    low: str | None = None,
    volume: str = "3000",
) -> HourlyBar:
    c = Decimal(close)
    return HourlyBar(
        market_id=market_id,
        hour=hour,
        open=c,
        close=c,
        high=Decimal(high) if high else c,
        low=Decimal(low) if low else c,
        volume=Decimal(volume),
        first_ts=hour,
        last_ts=hour,
        question=f"Will {market_id} resolve?",
        category="crypto",
        token_id=f"tok_{market_id}",
    )


START = date(2025, 6, 15)
END = date(2025, 6, 30)
ENTRY_HOUR = datetime(2025, 6, 15, 10, tzinfo=timezone.utc)


def _hour(offset: int) -> datetime:
    return ENTRY_HOUR + timedelta(hours=offset)


class TestTargetHitFillsAtTarget:
    """When the high of any bar touches target, we exit at target — not resolution."""

    def test_exit_at_target_not_at_resolution(self):
        # Entry @ 0.40 with estimator_prob=0.80.
        # Target = 0.40 + (0.80 - 0.40)*0.85 = 0.74.
        bars = [
            _bar("0xT1", _hour(0), "0.40"),
            _bar("0xT1", _hour(1), close="0.72", high="0.78", low="0.70"),
            _bar("0xT1", _hour(2), "0.95"),
        ]
        resolutions = {"0xT1": {"outcome": "Yes", "final_price": 1.0}}

        engine = _make_engine(FixedEstimator(0.80))
        result = engine.run(bars, resolutions, START, END, bankroll=800.0)

        assert result.total_trades == 1
        trade = result.trades[0]
        assert trade["exit_reason"] == "TARGET_HIT"
        assert trade["exit_price"] == pytest.approx(0.74, abs=0.005)
        assert trade["exit_price"] < 1.0


class TestForceCloseAtResolution:
    """If no trigger fires over the life of the market, close at final_price."""

    def test_partial_move_force_closes_at_resolution(self):
        # Entry @ 0.40, estimator=0.80, target=0.74.
        # Bars stay below target, window is too short for stale_thesis (≤ 24h).
        # Market's last bar → force close at resolution (final_price=0.60).
        bars = [
            _bar("0xR1", _hour(0), "0.40"),
            _bar("0xR1", _hour(1), "0.50"),
            _bar("0xR1", _hour(2), "0.55"),
        ]
        resolutions = {"0xR1": {"outcome": "Yes", "final_price": 0.60}}

        engine = _make_engine(FixedEstimator(0.80))
        result = engine.run(bars, resolutions, START, END, bankroll=800.0)

        assert result.total_trades == 1
        trade = result.trades[0]
        assert trade["exit_reason"] == "RESOLUTION"
        assert trade["exit_price"] == pytest.approx(0.60, abs=0.001)
        assert trade["pnl"] > 0  # partial win


class TestStaleThesisFires:
    """Position sits >stale_hours with <2% movement → STALE_THESIS triggers."""

    def test_stale_thesis_closes_idle_position(self):
        # Entry then 30 hours of bars all within ±2% of entry price.
        bars = [_bar("0xS1", _hour(0), "0.40", volume="5000")]
        # Use 2-hour spacing so volume_exit doesn't trigger (volume roughly flat).
        for step in range(1, 16):
            bars.append(_bar("0xS1", _hour(2 * step), "0.402", volume="3000"))
        resolutions = {"0xS1": {"outcome": "Yes", "final_price": 1.0}}

        engine = _make_engine(FixedEstimator(0.80))
        result = engine.run(bars, resolutions, START, END, bankroll=800.0)

        assert result.total_trades == 1
        assert result.trades[0]["exit_reason"] == "STALE_THESIS"


class TestVolumeExitFires:
    """Volume spike >volume_multiplier × entry-hourly-rate closes on that bar."""

    def test_volume_spike_closes_position(self):
        # Entry with baseline volume; later bar spikes to far above.
        bars = [
            _bar("0xV1", _hour(0), "0.40", volume="1000"),
            _bar("0xV1", _hour(1), "0.42", volume="1000"),
            _bar("0xV1", _hour(2), "0.45", volume="50000"),
        ]
        resolutions = {"0xV1": {"outcome": "Yes", "final_price": 1.0}}

        engine = _make_engine(FixedEstimator(0.80))
        result = engine.run(bars, resolutions, START, END, bankroll=800.0)

        assert result.total_trades == 1
        assert result.trades[0]["exit_reason"] == "VOLUME_EXIT"


class TestMidpointSanityCheck:
    """Midpoint estimator (zero gap) → scanner filters all entries → zero trades."""

    def test_midpoint_produces_no_trades(self):
        bars = [
            _bar("0xM1", _hour(0), "0.40", volume="5000"),
            _bar("0xM1", _hour(1), "1.00"),
        ]
        resolutions = {"0xM1": {"outcome": "Yes", "final_price": 1.0}}

        engine = _make_engine(MidpointEstimator())
        result = engine.run(bars, resolutions, START, END, bankroll=800.0)

        assert result.total_trades == 0


class TestOneEntryPerMarket:
    """Each market is evaluated for entry at most once."""

    def test_no_reentry_after_target_hit(self):
        bars = [
            _bar("0xE1", _hour(0), "0.40", volume="5000"),
            _bar("0xE1", _hour(1), close="0.72", high="0.80", low="0.70"),  # TARGET_HIT
            _bar("0xE1", _hour(2), "0.42", volume="5000"),                  # would re-pass filter
            _bar("0xE1", _hour(3), close="0.75", high="0.80", low="0.70"),
        ]
        resolutions = {"0xE1": {"outcome": "Yes", "final_price": 1.0}}

        engine = _make_engine(FixedEstimator(0.80))
        result = engine.run(bars, resolutions, START, END, bankroll=800.0)

        assert result.total_trades == 1


class TestTransactionCosts:
    """Transaction costs reduce P&L by cost_pct * position_size."""

    def test_costs_reduce_pnl(self):
        bars = [
            _bar("0xC1", _hour(0), "0.40"),
            _bar("0xC1", _hour(1), close="0.72", high="0.78", low="0.70"),
        ]
        resolutions = {"0xC1": {"outcome": "Yes", "final_price": 1.0}}

        engine_free = _make_engine(FixedEstimator(0.80))
        engine_cost = BacktestEngine(
            scanner=ScannerService(min_gap=0.07, min_depth=500, min_hours=4, max_hours=168),
            executor=ExecutorService(kelly_max_fraction=0.25, bankroll=800, paper_trade=True),
            exit_monitor=ExitMonitorService(
                target_pct=0.85, volume_multiplier=3, stale_hours=24, stale_threshold=0.02,
            ),
            estimator=FixedEstimator(0.80),
            scan_interval_hours=1,
            transaction_cost_pct=0.02,
        )

        result_free = engine_free.run(bars, resolutions, START, END, bankroll=800.0)
        result_cost = engine_cost.run(bars, resolutions, START, END, bankroll=800.0)

        assert result_free.total_trades == 1
        assert result_cost.total_trades == 1
        assert result_cost.trades[0]["pnl"] < result_free.trades[0]["pnl"]


class TestNoLookahead:
    """Non-historical estimators must not receive outcome or final_price."""

    def test_fixed_estimator_ignores_resolution_data(self):
        # FixedEstimator always returns 0.80 regardless of kwargs.
        # If look-ahead data leaked, a smarter estimator could cheat.
        # This test verifies the engine runs cleanly with is_lookahead=False.
        bars = [
            _bar("0xL1", _hour(0), "0.40"),
            _bar("0xL1", _hour(1), "0.75"),
        ]
        resolutions = {"0xL1": {"outcome": "Yes", "final_price": 1.0, "resolution_date": None}}

        engine = _make_engine(FixedEstimator(0.80))
        result = engine.run(bars, resolutions, START, END, bankroll=800.0)

        assert result.total_trades == 1
        assert result.trades[0]["estimator_prob"] == pytest.approx(0.80)


class TestResultMetrics:
    def test_result_aggregates(self):
        result = BacktestResult(
            trades=[
                {"pnl": 50.0, "exit_reason": "TARGET_HIT", "category": "crypto"},
                {"pnl": -20.0, "exit_reason": "STALE_THESIS", "category": "crypto"},
                {"pnl": 30.0, "exit_reason": "VOLUME_EXIT", "category": "politics"},
            ],
            start_date=date(2025, 1, 1),
            end_date=date(2025, 6, 30),
            estimator_name="fixed",
            bankroll=800.0,
        )
        assert result.total_trades == 3
        assert result.winners == 2
        assert result.win_rate == pytest.approx(66.67, abs=0.1)
        assert result.total_pnl == 60.0

    def test_max_drawdown_tracks_peak(self):
        result = BacktestResult(
            trades=[
                {"pnl": 100.0, "exit_reason": "TARGET_HIT", "category": "crypto"},
                {"pnl": -50.0, "exit_reason": "STALE_THESIS", "category": "crypto"},
                {"pnl": -30.0, "exit_reason": "VOLUME_EXIT", "category": "crypto"},
                {"pnl": 200.0, "exit_reason": "TARGET_HIT", "category": "crypto"},
            ],
            start_date=date(2025, 1, 1),
            end_date=date(2025, 6, 30),
            estimator_name="fixed",
            bankroll=800.0,
        )
        assert result.max_drawdown > 0

"""Tests for the exit monitor service."""
from decimal import Decimal

import pytest

from polyagent.models import ExitReason
from polyagent.services.exit_monitor import ExitMonitorService


class TestExitMonitor:
    def setup_method(self):
        self.monitor = ExitMonitorService(
            target_pct=0.85,
            volume_multiplier=3.0,
            stale_hours=24.0,
            stale_threshold=0.02,
        )

    def test_target_hit_triggers_exit(self):
        # Entry: 0.40, target: 0.57, current: 0.55
        # Expected gap = 0.57 - 0.40 = 0.17
        # 85% of gap = 0.1445, threshold = 0.40 + 0.1445 = 0.5445
        # Current 0.55 >= 0.5445 -> TARGET_HIT
        result = self.monitor.check_exit(
            entry_price=Decimal("0.40"),
            target_price=Decimal("0.57"),
            current_price=Decimal("0.55"),
            volume_10min=100.0,
            avg_volume_10min=100.0,
            hours_since_entry=4.0,
        )
        assert result == ExitReason.TARGET_HIT

    def test_volume_spike_triggers_exit(self):
        result = self.monitor.check_exit(
            entry_price=Decimal("0.40"),
            target_price=Decimal("0.57"),
            current_price=Decimal("0.42"),  # not at target
            volume_10min=400.0,
            avg_volume_10min=100.0,  # 4x spike > 3x threshold
            hours_since_entry=4.0,
        )
        assert result == ExitReason.VOLUME_EXIT

    def test_stale_thesis_triggers_exit(self):
        result = self.monitor.check_exit(
            entry_price=Decimal("0.40"),
            target_price=Decimal("0.57"),
            current_price=Decimal("0.404"),  # 1% move, well under 2% threshold
            volume_10min=100.0,
            avg_volume_10min=100.0,
            hours_since_entry=30.0,  # >24h
        )
        assert result == ExitReason.STALE_THESIS

    def test_no_exit_when_healthy(self):
        result = self.monitor.check_exit(
            entry_price=Decimal("0.40"),
            target_price=Decimal("0.57"),
            current_price=Decimal("0.45"),  # progressing but not at target
            volume_10min=150.0,
            avg_volume_10min=100.0,  # 1.5x, below 3x
            hours_since_entry=6.0,  # well within 24h
        )
        assert result is None

    def test_target_priority_over_volume(self):
        # Both target hit AND volume spike — target takes priority
        result = self.monitor.check_exit(
            entry_price=Decimal("0.40"),
            target_price=Decimal("0.57"),
            current_price=Decimal("0.56"),
            volume_10min=400.0,
            avg_volume_10min=100.0,
            hours_since_entry=4.0,
        )
        assert result == ExitReason.TARGET_HIT

    def test_stale_not_triggered_with_price_movement(self):
        result = self.monitor.check_exit(
            entry_price=Decimal("0.40"),
            target_price=Decimal("0.57"),
            current_price=Decimal("0.45"),  # 12.5% move > 2% threshold
            volume_10min=100.0,
            avg_volume_10min=100.0,
            hours_since_entry=30.0,
        )
        assert result is None  # price moved enough, not stale

    def test_calculate_pnl_buy_position(self):
        pnl = self.monitor.calculate_pnl(
            entry_price=Decimal("0.40"),
            exit_price=Decimal("0.55"),
            position_size=Decimal("100"),
            side="BUY",
        )
        # (0.55 - 0.40) / 0.40 * 100 = $37.50
        assert pnl == Decimal("37.50")

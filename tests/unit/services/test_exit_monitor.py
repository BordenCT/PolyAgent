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

    def test_calculate_pnl_sell_win(self):
        """Long NO at entry YES=0.40 (so NO cost basis = 0.60). Market resolves NO
        → YES exit ~ 0.00 → NO worth $1. Gain per share = (1-0.60)/0.60 = 66.67%."""
        pnl = self.monitor.calculate_pnl(
            entry_price=Decimal("0.40"),
            exit_price=Decimal("0.00"),
            position_size=Decimal("100"),
            side="SELL",
        )
        # (0.40 - 0.00) / (1 - 0.40) * 100 = $66.67
        assert pnl == Decimal("66.67")

    def test_calculate_pnl_sell_loss(self):
        """Long NO at entry YES=0.40. Market resolves YES (bad for us) → exit 1.00.
        Loss = 100% of position_size."""
        pnl = self.monitor.calculate_pnl(
            entry_price=Decimal("0.40"),
            exit_price=Decimal("1.00"),
            position_size=Decimal("100"),
            side="SELL",
        )
        # (0.40 - 1.00) / (1 - 0.40) * 100 = -$100.00
        assert pnl == Decimal("-100.00")

    def test_sell_target_hit(self):
        """SELL position: target below entry, fires when current <= threshold."""
        result = self.monitor.check_exit(
            entry_price=Decimal("0.40"),
            target_price=Decimal("0.15"),  # want YES to fall
            current_price=Decimal("0.18"),  # fell most of the way
            volume_10min=100.0,
            avg_volume_10min=100.0,
            hours_since_entry=4.0,
        )
        assert result == ExitReason.TARGET_HIT

    def test_resolved_yes_trigger(self):
        result = self.monitor.check_exit(
            entry_price=Decimal("0.40"),
            target_price=Decimal("0.15"),
            current_price=Decimal("0.998"),  # YES resolved
            volume_10min=100.0,
            avg_volume_10min=100.0,
            hours_since_entry=4.0,
            is_resolved=True,
        )
        assert result == ExitReason.RESOLVED_YES

    def test_empty_book_zero_price_does_not_fire_resolved_no(self):
        """Without is_resolved=True, a zero price is treated as a thin book,
        not a resolved market — prevents spurious close + re-entry loops."""
        result = self.monitor.check_exit(
            entry_price=Decimal("0.40"),
            target_price=Decimal("0.60"),
            current_price=Decimal("0.00"),  # empty book
            volume_10min=100.0,
            avg_volume_10min=100.0,
            hours_since_entry=4.0,
            is_resolved=False,
        )
        assert result is None

    def test_empty_book_does_not_fire_target_hit_on_sell(self):
        """SELL position: target well below entry, but current=0.00 is empty
        book, not a real price — TARGET_HIT must not fire."""
        result = self.monitor.check_exit(
            entry_price=Decimal("0.0815"),
            target_price=Decimal("0.02"),
            current_price=Decimal("0.00"),
            volume_10min=100.0,
            avg_volume_10min=100.0,
            hours_since_entry=4.0,
            is_resolved=False,
        )
        assert result is None

    def test_empty_book_still_allows_stale_thesis(self):
        """STALE should still fire after 24h even with untrusted book."""
        result = self.monitor.check_exit(
            entry_price=Decimal("0.40"),
            target_price=Decimal("0.60"),
            current_price=Decimal("0.404"),  # small drift
            volume_10min=100.0,
            avg_volume_10min=100.0,
            hours_since_entry=30.0,
            is_resolved=False,
        )
        assert result == ExitReason.STALE_THESIS

    def test_resolved_no_fires_only_when_flag_set(self):
        result = self.monitor.check_exit(
            entry_price=Decimal("0.40"),
            target_price=Decimal("0.60"),
            current_price=Decimal("0.002"),
            volume_10min=100.0,
            avg_volume_10min=100.0,
            hours_since_entry=4.0,
            is_resolved=True,
        )
        assert result == ExitReason.RESOLVED_NO

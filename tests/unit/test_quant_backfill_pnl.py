"""Tests for the pure transform helpers behind ``polyagent quant-backfill-pnl``.

The CLI itself is a thin wrapper around the database; the math we care
about lives in :func:`legacy_to_new` and :func:`is_already_new_format`.
Test those without touching a DB.
"""
from __future__ import annotations

from decimal import Decimal

from polyagent.cli.quant_backfill_pnl import (
    LegacyRowFix,
    is_already_new_format,
    legacy_to_new,
)


def _row(side, fill, size, outcome, pnl):
    return {
        "side": side,
        "fill_price_assumed": Decimal(fill),
        "size": Decimal(size),
        "outcome": outcome,
        "pnl": Decimal(pnl),
    }


class TestIsAlreadyNewFormat:
    def test_yes_win_legacy_format_detected(self):
        # Old: 5 * (1 - 0.40) = 3.00. New: 5 * (1-0.40)/0.40 = 7.50.
        # Stored pnl 3.00 matches OLD, not NEW -> legacy.
        row = _row("YES", "0.40", "5", "YES", "3.00")
        assert not is_already_new_format(row)

    def test_yes_win_new_format_detected(self):
        row = _row("YES", "0.40", "5", "YES", "7.50")
        assert is_already_new_format(row)

    def test_yes_loss_legacy_detected(self):
        # Old: -5 * 0.40 = -2.00. New: -5 (full stake).
        row = _row("YES", "0.40", "5", "NO", "-2.00")
        assert not is_already_new_format(row)

    def test_yes_loss_new_detected(self):
        row = _row("YES", "0.40", "5", "NO", "-5.00")
        assert is_already_new_format(row)

    def test_no_win_legacy_detected(self):
        # Legacy NO row stores fill=YES_bid=0.30, size=contracts*bid=0.90.
        # Old pnl = 0.90 * (1 - 0.30) = 0.63. New formula on uncorrected
        # fill+size would give 0.90 * 0.70/0.30 = 2.10, which doesn't match
        # 0.63 -> legacy.
        row = _row("NO", "0.30", "0.90", "NO", "0.63")
        assert not is_already_new_format(row)


class TestLegacyToNew:
    def test_yes_win_only_pnl_changes(self):
        # YES rows: fill (=ask) and size (=contracts*ask=stake) were
        # already correct. Only pnl needs recompute.
        row = _row("YES", "0.40", "5", "YES", "3.00")
        fix = legacy_to_new(row)
        assert fix.new_fill == Decimal("0.40")
        assert fix.new_size == Decimal("5")
        assert fix.new_pnl == Decimal("7.5")  # 5*(1-0.4)/0.4

    def test_yes_loss_only_pnl_changes(self):
        row = _row("YES", "0.40", "5", "NO", "-2.00")
        fix = legacy_to_new(row)
        assert fix.new_fill == Decimal("0.40")
        assert fix.new_size == Decimal("5")
        assert fix.new_pnl == Decimal("-5")

    def test_no_win_fill_and_size_transform(self):
        # Legacy NO at YES_bid=0.30 with contracts=3 stored size=0.90.
        # New fill = 1 - 0.30 = 0.70 (NO_ask). Contracts unchanged at 3,
        # so new size = 3 * 0.70 = 2.10. New pnl on NO win:
        # 2.10 * (1-0.70)/0.70 = 0.90 (= contracts * bid, as expected).
        row = _row("NO", "0.30", "0.90", "NO", "0.63")
        fix = legacy_to_new(row)
        assert fix.new_fill == Decimal("0.70")
        assert fix.new_size == Decimal("2.10")
        assert fix.new_pnl == Decimal("0.9")

    def test_no_loss_fill_and_size_transform(self):
        # Same legacy NO row but the trade lost (outcome=YES). New loss
        # = -new_size = -2.10 (the actual stake the trader lost).
        row = _row("NO", "0.30", "0.90", "YES", "-0.27")
        fix = legacy_to_new(row)
        assert fix.new_fill == Decimal("0.70")
        assert fix.new_size == Decimal("2.10")
        assert fix.new_pnl == Decimal("-2.10")

    def test_contracts_count_invariant(self):
        # Critical: the contract count (which is what the trader actually
        # held) must NOT change across the transform. Only the unit
        # convention used to record fill+size changes.
        row = _row("NO", "0.42", "1.26", "NO", "0.7308")
        fix = legacy_to_new(row)
        old_contracts = Decimal("1.26") / Decimal("0.42")  # 3
        new_contracts = fix.new_size / fix.new_fill
        assert old_contracts == new_contracts == Decimal("3")

    def test_returns_legacy_row_fix_dataclass(self):
        row = _row("YES", "0.40", "5", "YES", "3.00")
        fix = legacy_to_new(row)
        assert isinstance(fix, LegacyRowFix)

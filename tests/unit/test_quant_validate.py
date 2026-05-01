"""Unit tests for the validate_row helper used by `polyagent quant-validate`."""
from __future__ import annotations

from decimal import Decimal

from polyagent.cli.quant_validate import validate_row


def _row(side: str, fill: str, outcome: str, size: str, pnl: str) -> dict:
    return {
        "side": side,
        "fill_price_assumed": Decimal(fill),
        "size": Decimal(size),
        "our_outcome": outcome,
        "our_pnl": Decimal(pnl),
    }


def _pm(midpoint: str, resolved: bool = True) -> dict:
    return {"is_resolved": resolved, "midpoint_price": Decimal(midpoint)}


class TestMathSelfCheck:
    def test_correct_yes_win_passes(self):
        # YES at 0.40, outcome YES, size 5 -> +5*(1-0.40) = +3.00
        v = validate_row(_row("YES", "0.40", "YES", "5", "3.00"), pm_state=None)
        assert not v.math_mismatch

    def test_correct_no_win_passes(self):
        # NO at 0.55, outcome NO, size 5 -> +5*(1-0.55) = +2.25
        v = validate_row(_row("NO", "0.55", "NO", "5", "2.25"), pm_state=None)
        assert not v.math_mismatch

    def test_correct_loss_passes(self):
        # YES at 0.45, outcome NO, size 5 -> -5*0.45 = -2.25
        v = validate_row(_row("YES", "0.45", "NO", "5", "-2.25"), pm_state=None)
        assert not v.math_mismatch

    def test_corrupt_pnl_caught(self):
        # Stored pnl says +$10 but math says +$3. Catch it.
        v = validate_row(_row("YES", "0.40", "YES", "5", "10.00"), pm_state=None)
        assert v.math_mismatch


class TestPolymarketCrossCheck:
    def test_pm_agrees_no_mismatch(self):
        v = validate_row(
            _row("YES", "0.40", "YES", "5", "3.00"),
            pm_state=_pm("1"),
        )
        assert not v.math_mismatch
        assert not v.pm_mismatch
        assert v.pm_outcome == "YES"

    def test_pm_disagrees_flips_corrected_pnl(self):
        # We marked outcome YES (claimed +3.00 win); PM settled NO (-2.00 loss).
        v = validate_row(
            _row("YES", "0.40", "YES", "5", "3.00"),
            pm_state=_pm("0"),
        )
        assert not v.math_mismatch  # math is internally consistent
        assert v.pm_mismatch        # but PM says we lost
        assert v.pm_outcome == "NO"
        assert v.corrected_pnl == Decimal("-2.00")
        assert v.recorded_pnl == Decimal("3.00")

    def test_pm_unresolved_no_verdict(self):
        v = validate_row(
            _row("YES", "0.40", "YES", "5", "3.00"),
            pm_state=_pm("1", resolved=False),
        )
        assert v.pm_outcome is None
        assert not v.pm_mismatch
        assert v.corrected_pnl is None

    def test_pm_state_none_no_verdict(self):
        v = validate_row(_row("YES", "0.40", "YES", "5", "3.00"), pm_state=None)
        assert v.pm_outcome is None
        assert not v.pm_mismatch
        assert v.corrected_pnl is None

    def test_pm_midpoint_unrecognized_treated_as_unknown(self):
        # Midpoint that's neither 0 nor 1 (e.g. trading at 0.5 because PM
        # hasn't actually settled yet despite is_resolved being set).
        # We refuse to interpret it rather than guessing wrong.
        v = validate_row(
            _row("YES", "0.40", "YES", "5", "3.00"),
            pm_state=_pm("0.5"),
        )
        assert v.pm_outcome is None
        assert not v.pm_mismatch

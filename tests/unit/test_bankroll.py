"""Unit tests for the unified bankroll service."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from polyagent.services.bankroll import BankrollState, compute_bankroll_state


class TestBankrollStateAlgebra:
    def test_realized_total_sums_both_ledgers(self):
        s = BankrollState(
            starting=Decimal("20"),
            realized_main=Decimal("3"),
            realized_quant=Decimal("7"),
            open_capital_main=Decimal("0"),
            open_capital_quant=Decimal("0"),
        )
        assert s.realized_total == Decimal("10")

    def test_open_capital_total_sums_both_ledgers(self):
        s = BankrollState(
            starting=Decimal("20"),
            realized_main=Decimal("0"),
            realized_quant=Decimal("0"),
            open_capital_main=Decimal("5"),
            open_capital_quant=Decimal("8"),
        )
        assert s.open_capital_total == Decimal("13")

    def test_cumulative_is_starting_plus_realized(self):
        s = BankrollState(
            starting=Decimal("20"),
            realized_main=Decimal("3"),
            realized_quant=Decimal("7"),
            open_capital_main=Decimal("99"),  # ignored
            open_capital_quant=Decimal("99"),  # ignored
        )
        assert s.cumulative == Decimal("30")

    def test_free_is_cumulative_minus_open(self):
        s = BankrollState(
            starting=Decimal("20"),
            realized_main=Decimal("5"),
            realized_quant=Decimal("5"),
            open_capital_main=Decimal("4"),
            open_capital_quant=Decimal("6"),
        )
        # cumulative = 20 + 5 + 5 = 30; open = 4 + 6 = 10; free = 20.
        assert s.free == Decimal("20")

    def test_free_can_be_negative(self):
        # Over-leveraged paper: realized losses + many open trades > starting.
        s = BankrollState(
            starting=Decimal("20"),
            realized_main=Decimal("0"),
            realized_quant=Decimal("0"),
            open_capital_main=Decimal("0"),
            open_capital_quant=Decimal("125"),
        )
        assert s.free == Decimal("-105")


class TestComputeBankrollState:
    def _db_with(self, realized_main, realized_quant, open_main, open_quant):
        cur = MagicMock()
        cur.fetchone.return_value = {
            "realized_main": realized_main,
            "realized_quant": realized_quant,
            "open_main": open_main,
            "open_quant": open_quant,
        }
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        db = MagicMock()
        db.cursor.return_value = cur
        return db

    def test_zeros_yield_starting_equal_to_free(self):
        db = self._db_with(0, 0, 0, 0)
        s = compute_bankroll_state(db, Decimal("20"))
        assert s.starting == Decimal("20")
        assert s.free == Decimal("20")

    def test_passes_through_realized_and_open(self):
        db = self._db_with(realized_main=4, realized_quant=2, open_main=3, open_quant=1)
        s = compute_bankroll_state(db, Decimal("20"))
        assert s.realized_main == Decimal("4")
        assert s.realized_quant == Decimal("2")
        assert s.open_capital_main == Decimal("3")
        assert s.open_capital_quant == Decimal("1")
        assert s.free == Decimal("20") + Decimal("6") - Decimal("4")

    def test_handles_string_decimal_inputs(self):
        # psycopg returns Decimal-like strings; the service should stringify.
        db = self._db_with("1.50", "0.75", "0.25", "0.10")
        s = compute_bankroll_state(db, "20.00")
        assert s.realized_total == Decimal("2.25")
        assert s.open_capital_total == Decimal("0.35")
        assert s.free == Decimal("21.90")

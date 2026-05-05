"""Unit tests for the pure logic in `polyagent quant-reresolve`."""
from __future__ import annotations

from decimal import Decimal

from polyagent.cli.quant_reresolve import _market_pnl_delta, _pm_outcome


class TestPmOutcomeDecode:
    def test_resolved_yes(self):
        assert _pm_outcome({"is_resolved": True, "midpoint_price": Decimal("1")}) == "YES"

    def test_resolved_no(self):
        assert _pm_outcome({"is_resolved": True, "midpoint_price": Decimal("0")}) == "NO"

    def test_unresolved(self):
        assert _pm_outcome({"is_resolved": False, "midpoint_price": Decimal("0.5")}) is None

    def test_none_state(self):
        assert _pm_outcome(None) is None

    def test_unknown_midpoint(self):
        assert _pm_outcome({"is_resolved": True, "midpoint_price": Decimal("0.42")}) is None


def _trade(tid, side, fill, size, pnl):
    return {
        "id": tid,
        "side": side,
        "fill_price_assumed": Decimal(fill),
        "size": Decimal(size),
        "pnl": Decimal(pnl),
    }


class TestMarketPnlDelta:
    def test_flip_yes_to_no_recomputes_each_trade(self):
        # Old recorded outcome YES; PM says NO. Two trades on this market.
        # `size` is stake; new compute_pnl: win = size*(1-fill)/fill, loss = -size.
        # YES @ 0.40 stake $2: was +3.00 (win); under NO it's -2 (loss). Delta -5.
        # NO  @ 0.50 stake $5: was -5    (loss);   under NO it's +5 (win).   Delta +10.
        # Total delta: +5.
        trades = [
            _trade("ta", "YES", "0.40", "2", "3.00"),
            _trade("tb", "NO",  "0.50", "5", "-5"),
        ]
        total, updates = _market_pnl_delta(trades, "NO")
        assert total == Decimal("5")
        assert ("ta", Decimal("-2")) in updates
        assert ("tb", Decimal("5")) in updates

    def test_no_change_when_pm_outcome_matches_recorded(self):
        # If we feed in the same outcome the trades were resolved against,
        # delta is zero and updates equal the existing pnls.
        trades = [_trade("t1", "YES", "0.40", "2", "3.00")]
        total, updates = _market_pnl_delta(trades, "YES")
        assert total == Decimal("0")
        assert updates == [("t1", Decimal("3.00"))]

    def test_single_yes_trade_flipped_to_no_outcome(self):
        # Recorded as a YES win (+3.00) on $2 stake; under NO it's -2 (loss).
        # Delta = -5.00 (= -2 - 3).
        trades = [_trade("t1", "YES", "0.40", "2", "3.00")]
        total, updates = _market_pnl_delta(trades, "NO")
        assert total == Decimal("-5.00")
        assert updates == [("t1", Decimal("-2"))]

    def test_empty_trades_returns_zero(self):
        total, updates = _market_pnl_delta([], "YES")
        assert total == Decimal("0")
        assert updates == []

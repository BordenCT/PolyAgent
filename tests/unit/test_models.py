"""Tests for domain models."""
from decimal import Decimal

from polyagent.models import (
    ExitReason, MarketData, MarketStatus, PositionSide, PositionStatus,
    Score, ThesisChecks, Vote, VoteAction,
)


class TestScore:
    def test_create_score(self):
        score = Score(gap=0.12, depth=1500.0, hours=24.0, ev=0.18)
        assert score.gap == 0.12
        assert score.ev == 0.18

    def test_score_immutable(self):
        score = Score(gap=0.12, depth=1500.0, hours=24.0, ev=0.18)
        try:
            score.gap = 0.5
            assert False, "Should be frozen"
        except AttributeError:
            pass


class TestMarketData:
    def test_create_market(self):
        m = MarketData(
            polymarket_id="0x123", question="Will BTC hit 150k?",
            category="crypto", token_id="tok_abc",
            midpoint_price=Decimal("0.45"), bids_depth=Decimal("2000"),
            asks_depth=Decimal("1800"), hours_to_resolution=48.0,
            volume_24h=Decimal("150000"),
        )
        assert m.polymarket_id == "0x123"
        assert m.min_depth == Decimal("1800")

    def test_min_depth_returns_smaller_side(self):
        m = MarketData(
            polymarket_id="0x1", question="test", category="test",
            token_id="t1", midpoint_price=Decimal("0.5"),
            bids_depth=Decimal("500"), asks_depth=Decimal("800"),
            hours_to_resolution=10.0, volume_24h=Decimal("50000"),
        )
        assert m.min_depth == Decimal("500")


class TestVote:
    def test_buy_vote(self):
        v = Vote(action=VoteAction.BUY, confidence=0.82, reason="Strong convergence signal")
        assert v.action == VoteAction.BUY

    def test_hold_vote(self):
        v = Vote(action=VoteAction.HOLD, confidence=0.4, reason="Weak signal")
        assert v.action == VoteAction.HOLD


class TestThesisChecks:
    def test_count_passed(self):
        checks = ThesisChecks(base_rate=True, news=True, whale=False, disposition=True)
        assert checks.passed_count == 3

    def test_all_failed(self):
        checks = ThesisChecks(base_rate=False, news=False, whale=False, disposition=False)
        assert checks.passed_count == 0

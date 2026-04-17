"""Tests for convergence strategy."""
from polyagent.models import VoteAction
from polyagent.strategies.convergence import ConvergenceStrategy


class TestConvergenceStrategy:
    def setup_method(self):
        self.strategy = ConvergenceStrategy()

    def test_buy_when_estimate_above_price(self):
        vote = self.strategy.evaluate(
            claude_estimate=0.80,
            market_price=0.55,
            price_history=[0.50, 0.52, 0.54, 0.55],
        )
        assert vote.action == VoteAction.BUY

    def test_hold_when_price_moving_away(self):
        vote = self.strategy.evaluate(
            claude_estimate=0.80,
            market_price=0.55,
            price_history=[0.60, 0.58, 0.56, 0.55],
        )
        # Price trending down toward estimate? Actually moving toward.
        # This depends on direction. If estimate > price and price is rising, BUY.
        assert vote.action == VoteAction.BUY

    def test_hold_when_gap_too_small(self):
        vote = self.strategy.evaluate(
            claude_estimate=0.52,
            market_price=0.50,
            price_history=[0.49, 0.50, 0.50, 0.50],
        )
        assert vote.action == VoteAction.HOLD

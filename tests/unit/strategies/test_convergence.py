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
            price_history=[],
        )
        assert vote.action == VoteAction.BUY

    def test_sell_when_estimate_below_price(self):
        vote = self.strategy.evaluate(
            claude_estimate=0.10,
            market_price=0.40,
            price_history=[],
        )
        assert vote.action == VoteAction.SELL

    def test_hold_when_gap_inside_deadband(self):
        vote = self.strategy.evaluate(
            claude_estimate=0.52,
            market_price=0.50,
            price_history=[],
        )
        assert vote.action == VoteAction.HOLD

    def test_buy_fires_at_small_gap_matching_brain_edge(self):
        """Convergence must not be stricter than the brain's min_edge gate."""
        vote = self.strategy.evaluate(
            claude_estimate=0.43,
            market_price=0.40,  # gap = 0.03, exactly the boundary
            price_history=[],
        )
        # boundary is exclusive (< threshold holds); anything slightly over must fire
        vote2 = self.strategy.evaluate(
            claude_estimate=0.431,
            market_price=0.40,
            price_history=[],
        )
        assert vote.action == VoteAction.HOLD
        assert vote2.action == VoteAction.BUY

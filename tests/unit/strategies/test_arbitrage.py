"""Tests for arbitrage strategy."""
from polyagent.models import VoteAction
from polyagent.strategies.arbitrage import ArbitrageStrategy


class TestArbitrageStrategy:
    def setup_method(self):
        self.strategy = ArbitrageStrategy()

    def test_buy_when_related_market_diverges(self):
        vote = self.strategy.evaluate(
            claude_estimate=0.75,
            market_price=0.50,
            related_markets=[{"price": 0.72, "question": "related"}],
        )
        assert vote.action == VoteAction.BUY

    def test_hold_when_no_related_markets(self):
        vote = self.strategy.evaluate(
            claude_estimate=0.75,
            market_price=0.50,
            related_markets=[],
        )
        assert vote.action == VoteAction.HOLD

    def test_hold_when_prices_aligned(self):
        vote = self.strategy.evaluate(
            claude_estimate=0.55,
            market_price=0.50,
            related_markets=[{"price": 0.52, "question": "related"}],
        )
        assert vote.action == VoteAction.HOLD

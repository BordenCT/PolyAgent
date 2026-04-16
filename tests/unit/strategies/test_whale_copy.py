"""Tests for whale copy strategy."""
from polyagent.models import VoteAction
from polyagent.strategies.whale_copy import WhaleCopyStrategy


class TestWhaleCopyStrategy:
    def setup_method(self):
        self.strategy = WhaleCopyStrategy()

    def test_buy_when_whales_buying(self):
        vote = self.strategy.evaluate(
            whale_positions=[
                {"wallet": "0xabc", "side": "BUY", "size": 500},
                {"wallet": "0xdef", "side": "BUY", "size": 300},
            ],
            min_whale_count=2,
        )
        assert vote.action == VoteAction.BUY

    def test_hold_when_insufficient_whales(self):
        vote = self.strategy.evaluate(
            whale_positions=[
                {"wallet": "0xabc", "side": "BUY", "size": 500},
            ],
            min_whale_count=2,
        )
        assert vote.action == VoteAction.HOLD

    def test_hold_when_whales_disagree(self):
        vote = self.strategy.evaluate(
            whale_positions=[
                {"wallet": "0xabc", "side": "BUY", "size": 500},
                {"wallet": "0xdef", "side": "SELL", "size": 300},
            ],
            min_whale_count=2,
        )
        assert vote.action == VoteAction.HOLD

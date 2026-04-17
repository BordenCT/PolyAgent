"""Tests for the live order placement path in the executor."""
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

from polyagent.models import (
    MarketData,
    PositionSide,
    Thesis,
    ThesisChecks,
    Vote,
    VoteAction,
)
from polyagent.services.executor import ExecutorService


def _market() -> MarketData:
    return MarketData(
        polymarket_id="0xabc",
        question="Will X happen?",
        category="crypto",
        token_id="tok_1",
        midpoint_price=Decimal("0.65"),
        bids_depth=Decimal("1000"),
        asks_depth=Decimal("1000"),
        hours_to_resolution=48.0,
        volume_24h=Decimal("12000"),
    )


def _thesis(estimate: float = 0.82) -> Thesis:
    return Thesis.create(
        market_id=uuid4(),
        claude_estimate=estimate,
        confidence=0.85,
        checks=ThesisChecks(base_rate=True, news=True, whale=False, disposition=True),
        thesis_text="test",
    )


def _buy_votes() -> list[Vote]:
    return [
        Vote(action=VoteAction.BUY, confidence=0.8, reason="a"),
        Vote(action=VoteAction.BUY, confidence=0.7, reason="b"),
        Vote(action=VoteAction.HOLD, confidence=0.3, reason="c"),
    ]


class TestExecuteLive:
    def setup_method(self):
        self.executor = ExecutorService(kelly_max_fraction=0.25, bankroll=800.0, paper_trade=False)

    def test_live_order_success_returns_live_position(self):
        client = MagicMock()
        client.place_order.return_value = {
            "ok": True,
            "request": {"token_id": "tok_1"},
            "response": {"order_id": "o1", "price": 0.65},
        }
        trade_log = MagicMock()

        position = self.executor.execute_live(
            thesis=_thesis(),
            votes=_buy_votes(),
            market=_market(),
            polymarket_client=client,
            trade_log=trade_log,
        )

        assert position is not None
        assert position.paper_trade is False
        assert position.side == PositionSide.BUY
        assert position.volume_at_entry == Decimal("12000")
        client.place_order.assert_called_once()
        trade_log.insert.assert_called_once()
        call = trade_log.insert.call_args.kwargs
        assert call["action"] == "OPEN_LIVE"
        assert call["position_id"] == position.id

    def test_live_order_failure_returns_none_and_logs_error(self):
        client = MagicMock()
        client.place_order.return_value = {
            "ok": False,
            "request": {"token_id": "tok_1"},
            "error": "exit code 1",
            "stderr": "cli not found",
        }
        trade_log = MagicMock()

        position = self.executor.execute_live(
            thesis=_thesis(),
            votes=_buy_votes(),
            market=_market(),
            polymarket_client=client,
            trade_log=trade_log,
        )

        assert position is None
        trade_log.insert.assert_called_once()
        kwargs = trade_log.insert.call_args.kwargs
        assert kwargs["action"] == "OPEN_LIVE_FAILED"
        assert kwargs["error"] == "exit code 1"

    def test_live_no_consensus_skips_placement(self):
        client = MagicMock()
        trade_log = MagicMock()
        hold_votes = [
            Vote(action=VoteAction.HOLD, confidence=0.3, reason="a"),
            Vote(action=VoteAction.HOLD, confidence=0.2, reason="b"),
            Vote(action=VoteAction.HOLD, confidence=0.1, reason="c"),
        ]

        position = self.executor.execute_live(
            thesis=_thesis(),
            votes=hold_votes,
            market=_market(),
            polymarket_client=client,
            trade_log=trade_log,
        )

        assert position is None
        client.place_order.assert_not_called()
        trade_log.insert.assert_not_called()

    def test_paper_execute_still_works(self):
        position = self.executor.execute(
            thesis=_thesis(),
            votes=_buy_votes(),
            market_price=Decimal("0.65"),
            volume_at_entry=Decimal("9000"),
        )
        assert position is not None
        assert position.paper_trade is True
        assert position.volume_at_entry == Decimal("9000")

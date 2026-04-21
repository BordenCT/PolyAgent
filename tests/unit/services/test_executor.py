"""Tests for the executor service (Kelly sizing + consensus voting)."""
from decimal import Decimal
from uuid import uuid4

import pytest

from polyagent.models import (
    Consensus,
    Position,
    PositionSide,
    Thesis,
    ThesisChecks,
    Vote,
    VoteAction,
)
from polyagent.services.executor import ExecutorService


class TestKellySizing:
    def setup_method(self):
        self.executor = ExecutorService(
            kelly_max_fraction=0.25,
            bankroll=800.0,
            paper_trade=True,
        )

    def test_positive_ev_sizes_correctly(self):
        # Claude says 82%, market at 0.65, bankroll $800
        size = self.executor.kelly_size(p_win=0.82, market_price=0.65, bankroll=800.0)
        assert size > 0
        assert size <= 800 * 0.25  # capped at quarter kelly

    def test_negative_ev_returns_zero(self):
        # Claude says 30%, market at 0.65 — negative EV
        size = self.executor.kelly_size(p_win=0.30, market_price=0.65, bankroll=800.0)
        assert size == 0

    def test_capped_at_max_fraction(self):
        # Extremely confident — should still cap at 25%
        size = self.executor.kelly_size(p_win=0.99, market_price=0.10, bankroll=800.0)
        assert size == 800.0 * 0.25

    def test_known_values(self):
        # p=0.82, price=0.65 -> uncapped Kelly ~48.6%, capped at 25% -> $200
        size = self.executor.kelly_size(p_win=0.82, market_price=0.65, bankroll=800.0)
        assert 100 < size <= 200  # capped at quarter-kelly max ($200)

    def test_even_odds_fair_price_zero(self):
        # p=0.50, price=0.50 -> no edge, size should be 0
        size = self.executor.kelly_size(p_win=0.50, market_price=0.50, bankroll=800.0)
        assert size == 0


class TestConsensus:
    def setup_method(self):
        self.executor = ExecutorService(
            kelly_max_fraction=0.25,
            bankroll=800.0,
            paper_trade=True,
        )

    def test_two_buys_full_position(self):
        votes = [
            Vote(action=VoteAction.BUY, confidence=0.8, reason="arb"),
            Vote(action=VoteAction.BUY, confidence=0.7, reason="conv"),
            Vote(action=VoteAction.HOLD, confidence=0.3, reason="whale"),
        ]
        consensus, fraction, _side = self.executor.compute_consensus(votes)
        assert consensus == Consensus.FULL
        assert fraction == 1.0

    def test_one_buy_half_position(self):
        votes = [
            Vote(action=VoteAction.BUY, confidence=0.8, reason="arb"),
            Vote(action=VoteAction.HOLD, confidence=0.3, reason="conv"),
            Vote(action=VoteAction.HOLD, confidence=0.2, reason="whale"),
        ]
        consensus, fraction, _side = self.executor.compute_consensus(votes)
        assert consensus == Consensus.HALF
        assert fraction == 0.5

    def test_no_buys_no_trade(self):
        votes = [
            Vote(action=VoteAction.HOLD, confidence=0.3, reason="arb"),
            Vote(action=VoteAction.HOLD, confidence=0.2, reason="conv"),
            Vote(action=VoteAction.HOLD, confidence=0.1, reason="whale"),
        ]
        consensus, fraction, _side = self.executor.compute_consensus(votes)
        assert consensus == Consensus.NONE
        assert fraction == 0.0

    def test_three_buys_full_position(self):
        votes = [
            Vote(action=VoteAction.BUY, confidence=0.9, reason="arb"),
            Vote(action=VoteAction.BUY, confidence=0.8, reason="conv"),
            Vote(action=VoteAction.BUY, confidence=0.7, reason="whale"),
        ]
        consensus, fraction, _side = self.executor.compute_consensus(votes)
        assert consensus == Consensus.FULL
        assert fraction == 1.0


class TestExecute:
    def setup_method(self):
        self.executor = ExecutorService(
            kelly_max_fraction=0.25,
            bankroll=800.0,
            paper_trade=True,
        )

    def _make_thesis(self, estimate: float = 0.78) -> Thesis:
        return Thesis.create(
            market_id=uuid4(),
            claude_estimate=estimate,
            confidence=0.85,
            checks=ThesisChecks(base_rate=True, news=True, whale=False, disposition=True),
            thesis_text="test thesis",
        )

    def test_execute_full_consensus_creates_position(self):
        thesis = self._make_thesis(estimate=0.82)
        votes = [
            Vote(action=VoteAction.BUY, confidence=0.8, reason="arb"),
            Vote(action=VoteAction.BUY, confidence=0.7, reason="conv"),
            Vote(action=VoteAction.HOLD, confidence=0.3, reason="whale"),
        ]
        position = self.executor.execute(
            thesis=thesis,
            votes=votes,
            market_price=Decimal("0.65"),
        )
        assert position is not None
        assert position.paper_trade is True
        assert position.side == PositionSide.BUY
        assert float(position.position_size) > 0

    def test_execute_no_consensus_returns_none(self):
        thesis = self._make_thesis()
        votes = [
            Vote(action=VoteAction.HOLD, confidence=0.3, reason="arb"),
            Vote(action=VoteAction.HOLD, confidence=0.2, reason="conv"),
            Vote(action=VoteAction.HOLD, confidence=0.1, reason="whale"),
        ]
        position = self.executor.execute(
            thesis=thesis,
            votes=votes,
            market_price=Decimal("0.65"),
        )
        assert position is None

    def test_execute_sell_when_model_below_market(self):
        """Model p=0.10 vs market 0.40 — edge says buy NO at $0.60."""
        thesis = self._make_thesis(estimate=0.10)
        votes = [
            Vote(action=VoteAction.SELL, confidence=0.8, reason="conv"),
            Vote(action=VoteAction.HOLD, confidence=0.0, reason="arb"),
            Vote(action=VoteAction.HOLD, confidence=0.0, reason="whale"),
        ]
        position = self.executor.execute(
            thesis=thesis,
            votes=votes,
            market_price=Decimal("0.40"),
        )
        assert position is not None
        assert position.side == PositionSide.SELL
        assert float(position.position_size) > 0

    def test_conflicting_votes_skip(self):
        votes = [
            Vote(action=VoteAction.BUY, confidence=0.5, reason="conv"),
            Vote(action=VoteAction.SELL, confidence=0.5, reason="arb"),
            Vote(action=VoteAction.HOLD, confidence=0.0, reason="whale"),
        ]
        consensus, fraction, side = self.executor.compute_consensus(votes)
        assert consensus == Consensus.NONE
        assert side is None

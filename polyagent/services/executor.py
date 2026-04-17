"""Executor service — consensus voting and Kelly Criterion sizing."""
from __future__ import annotations

import logging
from decimal import Decimal

from polyagent.models import (
    Consensus,
    Position,
    PositionSide,
    Thesis,
    Vote,
    VoteAction,
)

logger = logging.getLogger("polyagent.services.executor")


class ExecutorService:
    """Handles consensus voting, position sizing, and trade execution."""

    def __init__(
        self,
        kelly_max_fraction: float = 0.25,
        bankroll: float = 800.0,
        paper_trade: bool = True,
    ) -> None:
        self._kelly_max_fraction = kelly_max_fraction
        self._bankroll = bankroll
        self._paper_trade = paper_trade

    def kelly_size(
        self,
        p_win: float,
        market_price: float,
        bankroll: float | None = None,
    ) -> float:
        """Calculate Kelly Criterion position size.

        Args:
            p_win: Estimated probability of winning (0-1).
            market_price: Current market price (0-1).
            bankroll: Total capital. Uses configured default if None.

        Returns:
            Dollar amount to bet. 0 if negative EV.
        """
        if bankroll is None:
            bankroll = self._bankroll

        if market_price <= 0 or market_price >= 1:
            return 0

        b = (1 / market_price) - 1  # payout ratio
        q = 1 - p_win  # loss probability
        f_star = (p_win * b - q) / b  # optimal fraction

        if f_star <= 0:
            return 0  # negative EV

        f_capped = min(f_star, self._kelly_max_fraction)
        return round(bankroll * f_capped, 2)

    def compute_consensus(self, votes: list[Vote]) -> tuple[Consensus, float]:
        """Compute consensus from strategy votes.

        Returns:
            Tuple of (consensus level, position fraction multiplier).
        """
        buy_votes = sum(1 for v in votes if v.action == VoteAction.BUY)

        if buy_votes >= 2:
            return Consensus.FULL, 1.0
        elif buy_votes == 1:
            return Consensus.HALF, 0.5
        else:
            return Consensus.NONE, 0.0

    def execute(
        self,
        thesis: Thesis,
        votes: list[Vote],
        market_price: Decimal,
    ) -> Position | None:
        """Execute a trade based on consensus and Kelly sizing.

        Returns:
            Position if trade executed, None if no consensus.
        """
        consensus, fraction = self.compute_consensus(votes)

        if consensus == Consensus.NONE:
            logger.info("SKIP — no consensus for market %s", thesis.market_id)
            return None

        # Update thesis with votes and consensus
        thesis.strategy_votes = {
            f"agent_{i}": v.action for i, v in enumerate(votes)
        }
        thesis.consensus = consensus

        # Calculate position size
        kelly_amount = self.kelly_size(
            p_win=thesis.claude_estimate,
            market_price=float(market_price),
        )
        position_size = round(kelly_amount * fraction, 2)

        if position_size <= 0:
            logger.info("SKIP — Kelly says no edge for market %s", thesis.market_id)
            return None

        # Calculate target price (entry + 85% of expected gap)
        expected_gap = thesis.claude_estimate - float(market_price)
        target_price = float(market_price) + (expected_gap * 0.85)

        position = Position.open_paper(
            thesis_id=thesis.id,
            market_id=thesis.market_id,
            side=PositionSide.BUY,
            entry_price=market_price,
            target_price=Decimal(str(round(target_price, 4))),
            kelly_fraction=round(kelly_amount / self._bankroll, 4),
            position_size=Decimal(str(position_size)),
        )

        mode = "PAPER" if self._paper_trade else "LIVE"
        logger.info(
            "%s %s %s — size=$%.2f kelly_f=%.3f consensus=%s",
            mode,
            position.side.value,
            thesis.market_id,
            position_size,
            position.kelly_fraction,
            consensus.value,
        )
        return position

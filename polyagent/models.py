"""Domain models for PolyAgent."""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from datetime import datetime, timezone
from uuid import UUID, uuid4


class MarketStatus(StrEnum):
    QUEUED = "queued"
    EVALUATING = "evaluating"
    REJECTED = "rejected"
    TRADED = "traded"

class PositionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"

class PositionSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

class ExitReason(StrEnum):
    TARGET_HIT = "TARGET_HIT"
    VOLUME_EXIT = "VOLUME_EXIT"
    STALE_THESIS = "STALE_THESIS"

class VoteAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

class Consensus(StrEnum):
    FULL = "full"
    HALF = "half"
    NONE = "none"


@dataclass(frozen=True)
class Score:
    """Scoring metrics for a candidate market opportunity.

    Args:
        gap: Price gap between market and estimated fair value.
        depth: Available liquidity depth in USD.
        hours: Hours remaining until market resolution.
        ev: Expected value of the trade opportunity.
    """
    gap: float
    depth: float
    hours: float
    ev: float


@dataclass
class MarketData:
    """Live market snapshot fetched from Polymarket.

    Args:
        polymarket_id: Unique market identifier (hex address).
        question: Human-readable market question.
        category: Market category (e.g. crypto, politics).
        token_id: Outcome token identifier for the YES side.
        midpoint_price: Current mid price between best bid and ask.
        bids_depth: Total USD available on the bid side.
        asks_depth: Total USD available on the ask side.
        hours_to_resolution: Hours until the market resolves.
        volume_24h: 24-hour trading volume in USD.
    """
    polymarket_id: str
    question: str
    category: str
    token_id: str
    midpoint_price: Decimal
    bids_depth: Decimal
    asks_depth: Decimal
    hours_to_resolution: float
    volume_24h: Decimal

    @property
    def min_depth(self) -> Decimal:
        """Returns the shallower side of the order book (liquidity constraint)."""
        return min(self.bids_depth, self.asks_depth)


@dataclass(frozen=True)
class Vote:
    """A single strategy agent's voting recommendation.

    Args:
        action: Recommended action (BUY, SELL, or HOLD).
        confidence: Agent confidence in the recommendation (0.0–1.0).
        reason: Human-readable rationale for the vote.
    """
    action: VoteAction
    confidence: float
    reason: str


@dataclass
class ThesisChecks:
    """Boolean checklist of thesis validation signals.

    Args:
        base_rate: Base rate analysis supports the trade.
        news: News sentiment supports the trade.
        whale: Whale activity supports the trade.
        disposition: Market disposition supports the trade.
    """
    base_rate: bool
    news: bool
    whale: bool
    disposition: bool

    @property
    def passed_count(self) -> int:
        """Returns the number of checks that passed."""
        return sum([self.base_rate, self.news, self.whale, self.disposition])


@dataclass
class Thesis:
    """Claude's full analysis and trading thesis for a market.

    Args:
        id: Unique thesis identifier.
        market_id: Associated market identifier.
        claude_estimate: Claude's estimated fair probability for YES.
        confidence: Claude's confidence in its estimate (0.0–1.0).
        checks: Checklist of supporting signals.
        thesis_text: Full written thesis from Claude.
        strategy_votes: Map of strategy name to vote action.
        consensus: Aggregate consensus across strategy votes.
        created_at: UTC timestamp when thesis was created.
    """
    id: UUID
    market_id: UUID
    claude_estimate: float
    confidence: float
    checks: ThesisChecks
    thesis_text: str
    strategy_votes: dict[str, VoteAction]
    consensus: Consensus
    created_at: datetime

    @staticmethod
    def create(
        market_id: UUID,
        claude_estimate: float,
        confidence: float,
        checks: ThesisChecks,
        thesis_text: str,
    ) -> Thesis:
        """Factory method to create a new Thesis with generated ID and timestamp.

        Args:
            market_id: The market this thesis analyzes.
            claude_estimate: Claude's probability estimate for YES.
            confidence: Claude's confidence level (0.0–1.0).
            checks: Completed thesis validation checklist.
            thesis_text: Full written thesis text.

        Returns:
            A new Thesis instance with no votes and NONE consensus.
        """
        return Thesis(
            id=uuid4(),
            market_id=market_id,
            claude_estimate=claude_estimate,
            confidence=confidence,
            checks=checks,
            thesis_text=thesis_text,
            strategy_votes={},
            consensus=Consensus.NONE,
            created_at=datetime.now(timezone.utc),
        )


@dataclass
class Position:
    """An open or closed trading position derived from a thesis.

    Args:
        id: Unique position identifier.
        thesis_id: The thesis that generated this position.
        market_id: The market being traded.
        side: Trade direction (BUY or SELL).
        entry_price: Price at which the position was opened.
        target_price: Price target for exit.
        kelly_fraction: Kelly criterion fraction used for sizing.
        position_size: Position size in USD.
        current_price: Most recent market price.
        status: Current position status (OPEN or CLOSED).
        exit_reason: Reason for close, or None if still open.
        pnl: Realized or unrealized profit/loss in USD.
        paper_trade: True if this is a simulated (paper) trade.
        opened_at: UTC timestamp when position was opened.
        closed_at: UTC timestamp when position was closed, or None.
    """
    id: UUID
    thesis_id: UUID
    market_id: UUID
    side: PositionSide
    entry_price: Decimal
    target_price: Decimal
    kelly_fraction: float
    position_size: Decimal
    current_price: Decimal
    status: PositionStatus
    exit_reason: ExitReason | None
    pnl: Decimal
    paper_trade: bool
    opened_at: datetime
    closed_at: datetime | None

    @staticmethod
    def open_paper(
        thesis_id: UUID,
        market_id: UUID,
        side: PositionSide,
        entry_price: Decimal,
        target_price: Decimal,
        kelly_fraction: float,
        position_size: Decimal,
    ) -> Position:
        """Factory method to open a new paper (simulated) position.

        Args:
            thesis_id: The thesis driving this trade.
            market_id: The market to trade.
            side: BUY or SELL.
            entry_price: Opening price.
            target_price: Desired exit price.
            kelly_fraction: Kelly fraction applied to sizing.
            position_size: USD size of the position.

        Returns:
            A new OPEN paper Position with zero PnL and no exit reason.
        """
        return Position(
            id=uuid4(),
            thesis_id=thesis_id,
            market_id=market_id,
            side=side,
            entry_price=entry_price,
            target_price=target_price,
            kelly_fraction=kelly_fraction,
            position_size=position_size,
            current_price=entry_price,
            status=PositionStatus.OPEN,
            exit_reason=None,
            pnl=Decimal("0"),
            paper_trade=True,
            opened_at=datetime.now(timezone.utc),
            closed_at=None,
        )

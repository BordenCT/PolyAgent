"""Executor service — consensus voting, Kelly sizing, and order placement."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from polyagent.models import (
    Consensus,
    MarketData,
    Position,
    PositionSide,
    Thesis,
    Vote,
    VoteAction,
)

if TYPE_CHECKING:
    from polyagent.data.clients.polymarket import PolymarketClient
    from polyagent.data.repositories.trade_log import TradeLogRepository

logger = logging.getLogger("polyagent.services.executor")


@dataclass(frozen=True)
class TradePlan:
    """A sized trade that passed consensus — ready to be opened (paper or live)."""
    consensus: Consensus
    side: PositionSide
    market_price: Decimal
    target_price: Decimal
    kelly_fraction: float
    position_size: Decimal


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

        b = (1 / market_price) - 1
        q = 1 - p_win
        f_star = (p_win * b - q) / b

        if f_star <= 0:
            return 0

        f_capped = min(f_star, self._kelly_max_fraction)
        return round(bankroll * f_capped, 2)

    def compute_consensus(
        self, votes: list[Vote]
    ) -> tuple[Consensus, float, PositionSide | None]:
        """Compute consensus and direction from strategy votes.

        Returns (consensus, fraction, side). Side is None when there's no trade:
        strategies conflict (BUY count == SELL count) or all HOLD.
        """
        buy_votes = sum(1 for v in votes if v.action == VoteAction.BUY)
        sell_votes = sum(1 for v in votes if v.action == VoteAction.SELL)

        if buy_votes == 0 and sell_votes == 0:
            return Consensus.NONE, 0.0, None
        if buy_votes == sell_votes:
            return Consensus.NONE, 0.0, None

        side = PositionSide.BUY if buy_votes > sell_votes else PositionSide.SELL
        dominant = max(buy_votes, sell_votes)
        if dominant >= 2:
            return Consensus.FULL, 1.0, side
        return Consensus.HALF, 0.5, side

    def plan(
        self,
        thesis: Thesis,
        votes: list[Vote],
        market_price: Decimal,
        current_bankroll: float | None = None,
    ) -> TradePlan | None:
        """Run consensus + Kelly sizing. Returns an intent to open, or None.

        Args:
            current_bankroll: Running equity for dynamic Kelly sizing. Falls back
                              to the configured bankroll if None.
        """
        consensus, fraction, side = self.compute_consensus(votes)

        if consensus == Consensus.NONE or side is None:
            logger.info("SKIP — no consensus for market %s", thesis.market_id)
            return None

        thesis.strategy_votes = {
            f"agent_{i}": v.action for i, v in enumerate(votes)
        }
        thesis.consensus = consensus

        market_p = float(market_price)
        if side == PositionSide.BUY:
            p_win = thesis.claude_estimate
            bet_price = market_p
        else:  # SELL = long NO
            p_win = 1.0 - thesis.claude_estimate
            bet_price = 1.0 - market_p

        kelly_amount = self.kelly_size(
            p_win=p_win,
            market_price=bet_price,
            bankroll=current_bankroll,
        )
        position_size = round(kelly_amount * fraction, 2)

        if position_size <= 0:
            logger.info("SKIP — Kelly says no edge for market %s", thesis.market_id)
            return None

        # Target price tracked in YES coordinates so exit_monitor and status work uniformly.
        # For SELL, the expected move is negative (YES price should fall toward estimate).
        expected_gap = thesis.claude_estimate - market_p
        target_price = market_p + (expected_gap * 0.85)

        effective_bankroll = current_bankroll if current_bankroll is not None else self._bankroll
        kelly_fraction = round(kelly_amount / effective_bankroll, 4) if effective_bankroll > 0 else 0.0

        return TradePlan(
            consensus=consensus,
            side=side,
            market_price=market_price,
            target_price=Decimal(str(round(target_price, 4))),
            kelly_fraction=kelly_fraction,
            position_size=Decimal(str(position_size)),
        )

    def execute(
        self,
        thesis: Thesis,
        votes: list[Vote],
        market_price: Decimal,
        volume_at_entry: Decimal = Decimal("0"),
        current_bankroll: float | None = None,
    ) -> Position | None:
        """Plan and open a paper position. Returns None if no trade is taken."""
        plan = self.plan(thesis, votes, market_price, current_bankroll=current_bankroll)
        if plan is None:
            return None

        position = Position.open_paper(
            thesis_id=thesis.id,
            market_id=thesis.market_id,
            side=plan.side,
            entry_price=plan.market_price,
            target_price=plan.target_price,
            kelly_fraction=plan.kelly_fraction,
            position_size=plan.position_size,
            volume_at_entry=volume_at_entry,
        )

        logger.info(
            "PAPER %s %s — size=$%.2f kelly_f=%.3f consensus=%s",
            position.side.value, thesis.market_id,
            float(position.position_size), position.kelly_fraction,
            plan.consensus.value,
        )
        return position

    def execute_live(
        self,
        thesis: Thesis,
        votes: list[Vote],
        market: MarketData,
        polymarket_client: "PolymarketClient",
        trade_log: "TradeLogRepository | None" = None,
        current_bankroll: float | None = None,
    ) -> Position | None:
        """Plan, place a real order via the CLOB, and return the opened position.

        Returns None if consensus fails, Kelly rejects, or order placement fails.
        On placement failure, the attempt is recorded to trade_log (if provided)
        against a synthesized position ID so the error is auditable.
        """
        plan = self.plan(thesis, votes, market.midpoint_price, current_bankroll=current_bankroll)
        if plan is None:
            return None

        result = polymarket_client.place_order(
            token_id=market.token_id,
            side=plan.side.value,
            price=float(plan.market_price),
            size=float(plan.position_size),
        )

        if not result.get("ok"):
            logger.error(
                "LIVE %s FAILED — %s (market=%s)",
                plan.side.value, result.get("error"), thesis.market_id,
            )
            if trade_log is not None:
                from uuid import uuid4
                trade_log.insert(
                    position_id=uuid4(),
                    action="OPEN_LIVE_FAILED",
                    reason=str(thesis.market_id),
                    raw_request=result.get("request"),
                    raw_response=result.get("response"),
                    error=result.get("error"),
                )
            return None

        fill_price = _extract_fill_price(result.get("response"), plan.market_price)

        position = Position.open_live(
            thesis_id=thesis.id,
            market_id=thesis.market_id,
            side=plan.side,
            entry_price=fill_price,
            target_price=plan.target_price,
            kelly_fraction=plan.kelly_fraction,
            position_size=plan.position_size,
            volume_at_entry=market.volume_24h,
        )

        if trade_log is not None:
            trade_log.insert(
                position_id=position.id,
                action="OPEN_LIVE",
                reason=plan.consensus.value,
                raw_request=result.get("request"),
                raw_response=result.get("response"),
            )

        logger.info(
            "LIVE %s %s — size=$%.2f fill=%.4f kelly_f=%.3f consensus=%s",
            position.side.value, thesis.market_id,
            float(position.position_size), float(fill_price),
            position.kelly_fraction, plan.consensus.value,
        )
        return position


def _extract_fill_price(response: dict | None, fallback: Decimal) -> Decimal:
    """Pull the filled price out of the CLOB response, falling back to limit price."""
    if not response:
        return fallback
    raw = response.get("price") or response.get("avg_price") or response.get("fill_price")
    if raw is None:
        return fallback
    try:
        return Decimal(str(raw))
    except (TypeError, ValueError):
        return fallback

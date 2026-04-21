"""Convergence strategy — votes based on gap between Claude's estimate and market price."""
from __future__ import annotations

from polyagent.models import Vote, VoteAction

GAP_THRESHOLD = 0.03


class ConvergenceStrategy:
    """Votes BUY when the model thinks YES is underpriced, SELL when overpriced.

    The gap threshold matches the brain's min_edge gate so any thesis the brain
    lets through gets a direction vote from this strategy.
    """

    name: str = "convergence"

    def evaluate(
        self,
        claude_estimate: float,
        market_price: float,
        price_history: list[float],
    ) -> Vote:
        gap = claude_estimate - market_price

        if abs(gap) < GAP_THRESHOLD:
            return Vote(
                action=VoteAction.HOLD,
                confidence=0.0,
                reason=f"Gap {gap:+.3f} inside {GAP_THRESHOLD:.2f} dead band",
            )

        if gap > 0:
            return Vote(
                action=VoteAction.BUY,
                confidence=min(gap * 3, 1.0),
                reason=f"Model long YES: est={claude_estimate:.2f} vs price={market_price:.2f}",
            )

        return Vote(
            action=VoteAction.SELL,
            confidence=min(-gap * 3, 1.0),
            reason=f"Model long NO: est={claude_estimate:.2f} vs price={market_price:.2f}",
        )

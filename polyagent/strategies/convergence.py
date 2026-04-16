"""Convergence strategy — enters when price moves toward Claude's estimate."""
from __future__ import annotations

from polyagent.models import Vote, VoteAction


class ConvergenceStrategy:
    """Enters positions when price is trending toward Claude's probability estimate."""

    name: str = "convergence"

    def evaluate(
        self,
        claude_estimate: float,
        market_price: float,
        price_history: list[float],
    ) -> Vote:
        gap = claude_estimate - market_price

        # Need at least a 5% gap to act
        if abs(gap) < 0.05:
            return Vote(
                action=VoteAction.HOLD,
                confidence=0.0,
                reason=f"Gap too small ({gap:.3f}) for convergence play",
            )

        # Check trend direction from price history
        if len(price_history) >= 2:
            recent_trend = price_history[-1] - price_history[0]
            # Price trending toward estimate = convergence signal
            if gap > 0 and recent_trend >= 0:
                return Vote(
                    action=VoteAction.BUY,
                    confidence=min(abs(gap) * 4, 1.0),
                    reason=f"Price trending up toward estimate "
                    f"(est={claude_estimate:.2f}, price={market_price:.2f})",
                )

        # Large gap alone is enough with no counter-trend
        if gap > 0.10:
            return Vote(
                action=VoteAction.BUY,
                confidence=min(gap * 3, 1.0),
                reason=f"Large gap: estimate {claude_estimate:.2f} vs price {market_price:.2f}",
            )

        return Vote(
            action=VoteAction.HOLD,
            confidence=0.0,
            reason="No convergence signal detected",
        )

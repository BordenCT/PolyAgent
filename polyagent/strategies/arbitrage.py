"""Arbitrage strategy — catches price gaps between related markets."""
from __future__ import annotations

from polyagent.models import Vote, VoteAction


class ArbitrageStrategy:
    """Detects price discrepancies between semantically related markets."""

    name: str = "arbitrage"

    def evaluate(
        self,
        claude_estimate: float,
        market_price: float,
        related_markets: list[dict],
    ) -> Vote:
        if not related_markets:
            return Vote(
                action=VoteAction.HOLD,
                confidence=0.0,
                reason="No related markets found for arbitrage comparison",
            )

        # Check if any related market's price diverges from ours by > 10%
        for related in related_markets:
            related_price = related.get("price", market_price)
            gap = abs(related_price - market_price)
            if gap > 0.10 and claude_estimate > market_price:
                return Vote(
                    action=VoteAction.BUY,
                    confidence=min(gap * 5, 1.0),
                    reason=f"Related market at {related_price:.2f} vs {market_price:.2f} "
                    f"(gap={gap:.2f})",
                )

        return Vote(
            action=VoteAction.HOLD,
            confidence=0.0,
            reason="Related market prices aligned, no arbitrage opportunity",
        )

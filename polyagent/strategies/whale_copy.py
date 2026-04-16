"""Whale copy strategy — mirrors target wallet positions."""
from __future__ import annotations

from polyagent.models import Vote, VoteAction


class WhaleCopyStrategy:
    """Mirrors positions of high-performing target wallets."""

    name: str = "whale_copy"

    def evaluate(
        self,
        whale_positions: list[dict],
        min_whale_count: int = 2,
    ) -> Vote:
        if not whale_positions:
            return Vote(
                action=VoteAction.HOLD,
                confidence=0.0,
                reason="No whale positions detected",
            )

        buy_whales = [w for w in whale_positions if w.get("side") == "BUY"]
        sell_whales = [w for w in whale_positions if w.get("side") == "SELL"]

        if len(buy_whales) >= min_whale_count and len(buy_whales) > len(sell_whales):
            total_size = sum(w.get("size", 0) for w in buy_whales)
            return Vote(
                action=VoteAction.BUY,
                confidence=min(len(buy_whales) / 5, 1.0),
                reason=f"{len(buy_whales)} whales buying (total ${total_size})",
            )

        return Vote(
            action=VoteAction.HOLD,
            confidence=0.0,
            reason=f"Insufficient whale consensus "
            f"({len(buy_whales)} buy, {len(sell_whales)} sell)",
        )

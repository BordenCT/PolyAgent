"""Market scanning and scoring service."""
from __future__ import annotations

import logging
import re
from decimal import Decimal

from polyagent.models import MarketData, Score

logger = logging.getLogger("polyagent.services.scanner")


# Patterns the brain has shown it cannot reason about (crypto strike ladders,
# narrow price-range buckets). Buying these is a guaranteed loss because the
# model has no live price reference and overestimates tail probabilities.
DEFAULT_QUESTION_BLOCKLIST: tuple[str, ...] = (
    # Barrier-touch markets ("dip to", "reach") need running-min/max math
    # we haven't shipped yet. Kill until that's built.
    r"^Will (Bitcoin|Ethereum|Solana) (reach|dip to|hit)",
    # Verbose-slug daily up/down markets (e.g. "Bitcoin Up or Down on April 30",
    # "S&P 500 (SPX) Up or Down on April 30", "WTI Crude Oil (WTI) Opens Up or
    # Down on April 30"). These are directional binary bets where the LLM has
    # no information edge. Phase 2 work will route the BTC/ETH variants to
    # QuantStrikeService with a snapshotted opening spot. For now: block.
    r"\b(Up or Down|Opens Up or Down)\b on ",
)


class ScannerService:
    """Scores markets against configurable thresholds, killing ~93% of candidates."""

    def __init__(
        self,
        min_gap: float,
        min_depth: float,
        min_hours: float,
        max_hours: float,
        min_price: float = 0.02,
        max_price: float = 0.98,
        question_blocklist: tuple[str, ...] = DEFAULT_QUESTION_BLOCKLIST,
    ) -> None:
        self._min_gap = min_gap
        self._min_depth = min_depth
        self._min_hours = min_hours
        self._max_hours = max_hours
        self._min_price = min_price
        self._max_price = max_price
        self._blocklist = tuple(re.compile(p, re.IGNORECASE) for p in question_blocklist)

    def _is_blocked(self, question: str) -> bool:
        return any(p.search(question or "") for p in self._blocklist)

    def score_market(
        self, market: MarketData, historical_estimate: float
    ) -> Score | None:
        """Score a market against kill filters. Returns None if killed.

        Args:
            market: The market snapshot to evaluate.
            historical_estimate: External probability estimate for the YES outcome.

        Returns:
            A Score if the market passes all filters, None otherwise.
        """
        price = float(market.midpoint_price)
        gap = abs(historical_estimate - price)
        depth = float(market.min_depth)
        hours_left = market.hours_to_resolution

        # Kill filters
        if self._is_blocked(market.question):
            logger.debug("KILL %s — blocklisted: %s", market.polymarket_id, market.question[:60])
            return None
        if gap <= self._min_gap:
            logger.debug("KILL %s — gap %.3f too thin", market.polymarket_id, gap)
            return None
        if depth < self._min_depth:
            logger.debug("KILL %s — depth %.0f can't fill", market.polymarket_id, depth)
            return None
        if hours_left < self._min_hours:
            logger.debug("KILL %s — %.1fh too late", market.polymarket_id, hours_left)
            return None
        if hours_left > self._max_hours:
            logger.debug("KILL %s — %.1fh too slow", market.polymarket_id, hours_left)
            return None

        ev = round(gap * depth * 0.001, 2)
        return Score(
            gap=round(gap, 3),
            depth=depth,
            hours=hours_left,
            ev=ev,
        )

    def scan_batch(
        self, markets: list[MarketData], estimates: dict[str, float]
    ) -> list[tuple[MarketData, Score]]:
        """Score a batch of markets, returning only survivors with their scores.

        Args:
            markets: List of market snapshots to evaluate.
            estimates: Map of polymarket_id to historical probability estimate.
                       Falls back to the market's own midpoint if no estimate exists.

        Returns:
            List of (market, score) tuples for markets that passed all filters.
        """
        survivors = []
        killed_gap = killed_depth = killed_hours = killed_price = killed_blocked = 0
        for market in markets:
            # Fallback to 0.5 (neutral prior), NOT midpoint — midpoint fallback zeros the gap
            estimate = estimates.get(market.polymarket_id, 0.5)
            price = float(market.midpoint_price)
            gap = abs(estimate - price)
            depth = float(market.min_depth)
            hours = market.hours_to_resolution

            if self._is_blocked(market.question):
                killed_blocked += 1
                continue
            if price < self._min_price or price > self._max_price:
                killed_price += 1
                continue
            if gap <= self._min_gap:
                killed_gap += 1
                continue
            if depth < self._min_depth:
                killed_depth += 1
                continue
            if hours < self._min_hours or hours > self._max_hours:
                killed_hours += 1
                continue

            ev = round(gap * depth * 0.001, 2)
            survivors.append((market, Score(gap=round(gap, 3), depth=depth, hours=hours, ev=ev)))

        logger.info(
            "Scanned %d markets -> %d survivors | killed: blocklist=%d price=%d gap=%d depth=%d hours=%d",
            len(markets),
            len(survivors),
            killed_blocked,
            killed_price,
            killed_gap,
            killed_depth,
            killed_hours,
        )
        return survivors

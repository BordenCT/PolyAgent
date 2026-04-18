"""Market scanning and scoring service."""
from __future__ import annotations

import logging
from decimal import Decimal

from polyagent.models import MarketData, Score

logger = logging.getLogger("polyagent.services.scanner")


class ScannerService:
    """Scores markets against configurable thresholds, killing ~93% of candidates."""

    def __init__(
        self,
        min_gap: float,
        min_depth: float,
        min_hours: float,
        max_hours: float,
    ) -> None:
        self._min_gap = min_gap
        self._min_depth = min_depth
        self._min_hours = min_hours
        self._max_hours = max_hours

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
        killed_gap = killed_depth = killed_hours = 0
        for market in markets:
            # Fallback to 0.5 (neutral prior), NOT midpoint — midpoint fallback zeros the gap
            estimate = estimates.get(market.polymarket_id, 0.5)
            price = float(market.midpoint_price)
            gap = abs(estimate - price)
            depth = float(market.min_depth)
            hours = market.hours_to_resolution

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
            "Scanned %d markets -> %d survivors | killed: gap=%d depth=%d hours=%d",
            len(markets),
            len(survivors),
            killed_gap,
            killed_depth,
            killed_hours,
        )
        return survivors

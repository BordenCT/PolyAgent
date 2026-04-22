"""Exit monitor service — 3-trigger exit system."""
from __future__ import annotations

import logging
from decimal import Decimal

from polyagent.models import ExitReason

logger = logging.getLogger("polyagent.services.exit_monitor")


class ExitMonitorService:
    """Monitors open positions and fires exit triggers."""

    def __init__(
        self,
        target_pct: float = 0.85,
        volume_multiplier: float = 3.0,
        stale_hours: float = 24.0,
        stale_threshold: float = 0.02,
        resolved_no_threshold: float = 0.005,
    ) -> None:
        self._target_pct = target_pct
        self._volume_multiplier = volume_multiplier
        self._stale_hours = stale_hours
        self._stale_threshold = stale_threshold
        self._resolved_no_threshold = resolved_no_threshold

    @property
    def target_pct(self) -> float:
        return self._target_pct

    @property
    def volume_multiplier(self) -> float:
        return self._volume_multiplier

    @property
    def stale_hours(self) -> float:
        return self._stale_hours

    @property
    def stale_threshold(self) -> float:
        return self._stale_threshold

    def check_exit(
        self,
        entry_price: Decimal,
        target_price: Decimal,
        current_price: Decimal,
        volume_10min: float,
        avg_volume_10min: float,
        hours_since_entry: float,
        is_resolved: bool = False,
    ) -> ExitReason | None:
        """Check all exit triggers. Returns reason or None.

        All prices are YES-coord: entry_price and current_price track the YES
        outcome price so BUY (long YES) and SELL (long NO) share the same
        monitoring logic — direction is inferred from target vs entry.

        is_resolved MUST come from an authoritative source (CLOB's closed/
        archived flag). A midpoint near 0 or 1 alone is not enough — thin or
        briefly empty order books push midpoint to 0 without the market being
        resolved, and an early RESOLVED_NO close then lets the scanner re-enter
        the same market on the next sweep.

        Trigger priority: TARGET_HIT > RESOLVED_YES/NO > VOLUME_EXIT > STALE_THESIS
        """
        current = float(current_price)
        entry = float(entry_price)
        high_cutoff = 1.0 - self._resolved_no_threshold

        if is_resolved:
            # 0a. YES resolved — price pinned near 1.0
            if current >= high_cutoff and entry < high_cutoff:
                logger.info(
                    "RESOLVED_YES: current=%.4f >= %.4f threshold",
                    current, high_cutoff,
                )
                return ExitReason.RESOLVED_YES
            # 0b. NO resolved — price pinned near 0.0
            if current <= self._resolved_no_threshold and entry > self._resolved_no_threshold:
                logger.info(
                    "RESOLVED_NO: current=%.4f <= %.4f threshold",
                    current, self._resolved_no_threshold,
                )
                return ExitReason.RESOLVED_NO

        # Book at either extreme without a resolution flag is almost always a
        # thin/empty book, not a legitimate price. Skip every price-based
        # trigger (TARGET_HIT, VOLUME_EXIT). Let STALE_THESIS handle eventual
        # cleanup if it really is stuck.
        book_untrusted = (not is_resolved) and (
            current <= self._resolved_no_threshold or current >= high_cutoff
        )

        if not book_untrusted:
            # 1. Target hit — 85% of expected move captured (direction-aware)
            expected_gap = float(target_price - entry_price)
            if expected_gap > 0:  # BUY: YES should rise toward target
                threshold = entry + (expected_gap * self._target_pct)
                if current >= threshold:
                    logger.info(
                        "TARGET_HIT (BUY): current=%.4f >= threshold=%.4f",
                        current, threshold,
                    )
                    return ExitReason.TARGET_HIT
            elif expected_gap < 0:  # SELL: YES should fall toward target
                threshold = entry + (expected_gap * self._target_pct)
                if current <= threshold:
                    logger.info(
                        "TARGET_HIT (SELL): current=%.4f <= threshold=%.4f",
                        current, threshold,
                    )
                    return ExitReason.TARGET_HIT

            # 2. Volume spike — 3x normal = smart money leaving
            if avg_volume_10min > 0 and volume_10min > avg_volume_10min * self._volume_multiplier:
                logger.info(
                    "VOLUME_EXIT: vol_10m=%.0f > %.0f (%.1fx avg)",
                    volume_10min,
                    avg_volume_10min * self._volume_multiplier,
                    volume_10min / avg_volume_10min,
                )
                return ExitReason.VOLUME_EXIT

        # 3. Time decay — thesis stale after 24h with < 2% price movement
        if hours_since_entry > self._stale_hours:
            price_change = abs(float(current_price - entry_price) / float(entry_price))
            if price_change < self._stale_threshold:
                logger.info(
                    "STALE_THESIS: %.1fh elapsed, price change=%.3f < %.3f threshold",
                    hours_since_entry,
                    price_change,
                    self._stale_threshold,
                )
                return ExitReason.STALE_THESIS

        return None

    def calculate_pnl(
        self,
        entry_price: Decimal,
        exit_price: Decimal,
        position_size: Decimal,
        side: str,
    ) -> Decimal:
        """Calculate realized P&L for a closed position.

        All prices are in YES coordinates. For SELL (long NO), the bettor paid
        (1 - entry_yes) per share, so the denominator is (1 - entry_yes), not
        entry_yes. Using entry_yes would over-report gains when entry YES < 0.5.
        """
        entry = Decimal(str(entry_price))
        exit_p = Decimal(str(exit_price))
        size = Decimal(str(position_size))
        if side == "BUY":
            if entry == 0:
                return Decimal("0")
            pct_change = (exit_p - entry) / entry
        else:  # SELL = long NO; cost basis = (1 - entry_yes)
            denom = Decimal("1") - entry
            if denom == 0:
                return Decimal("0")
            pct_change = (entry - exit_p) / denom
        return (pct_change * size).quantize(Decimal("0.01"))

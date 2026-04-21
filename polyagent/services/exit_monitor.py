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
    ) -> ExitReason | None:
        """Check all 4 exit triggers. Returns reason or None.

        Trigger priority: TARGET_HIT > RESOLVED_NO > VOLUME_EXIT > STALE_THESIS
        """
        # 0. Market resolved NO — price near zero means the bet lost; close immediately
        # (STALE_THESIS wouldn't catch this because 100% price drop exceeds stale_threshold)
        if float(current_price) <= self._resolved_no_threshold and float(entry_price) > self._resolved_no_threshold:
            logger.info(
                "RESOLVED_NO: current=%.4f <= %.4f threshold",
                float(current_price),
                self._resolved_no_threshold,
            )
            return ExitReason.RESOLVED_NO

        # 1. Target hit — 85% of expected move captured
        expected_gap = float(target_price - entry_price)
        if expected_gap > 0:
            threshold = float(entry_price) + (expected_gap * self._target_pct)
            if float(current_price) >= threshold:
                logger.info(
                    "TARGET_HIT: current=%.4f >= threshold=%.4f",
                    float(current_price),
                    threshold,
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
        """Calculate realized P&L for a closed position."""
        if side == "BUY":
            pct_change = (exit_price - entry_price) / entry_price
        else:
            pct_change = (entry_price - exit_price) / entry_price
        return (pct_change * position_size).quantize(Decimal("0.01"))

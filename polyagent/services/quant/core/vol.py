"""Per-tenor vol calibration policy.

`compute_vol(spec, source, horizon_s)` is the single call site. Decider
and strike service both use it. Three methods:

- ROLLING_REALIZED: lookback = clamp(min, k * horizon, max); fall back
  to default_vol if rolling returns 0 (insufficient samples).
- FIXED: return fixed_value or default_vol.
- HYBRID: ROLLING_REALIZED for short horizons, FIXED past
  hybrid_threshold_s.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class VolMethod(str, Enum):
    ROLLING_REALIZED = "ROLLING_REALIZED"
    FIXED = "FIXED"
    HYBRID = "HYBRID"


@dataclass(frozen=True)
class VolCalibration:
    method: VolMethod
    rolling_min_s: int = 300
    rolling_max_s: int = 24 * 3600
    rolling_horizon_multiplier: float = 4.0
    fixed_value: float | None = None
    hybrid_threshold_s: int = 4 * 3600


class _SupportsRollingVol(Protocol):
    def realized_vol(self, window_s: int) -> float: ...


class _SpecLike(Protocol):
    @property
    def default_vol(self) -> float: ...
    @property
    def vol_calibration(self) -> VolCalibration: ...


def _rolling(spec: _SpecLike, source: _SupportsRollingVol, horizon_s: float) -> float:
    cal = spec.vol_calibration
    raw = horizon_s * cal.rolling_horizon_multiplier
    lookback = max(cal.rolling_min_s, min(int(raw), cal.rolling_max_s))
    v = source.realized_vol(lookback)
    return v if v > 0 else spec.default_vol


def _fixed(spec: _SpecLike) -> float:
    return spec.vol_calibration.fixed_value if spec.vol_calibration.fixed_value is not None else spec.default_vol


def compute_vol(spec: _SpecLike, source: _SupportsRollingVol, horizon_s: float) -> float:
    """Return annualised sigma for `spec` over `horizon_s`."""
    cal = spec.vol_calibration
    if cal.method == VolMethod.FIXED:
        return _fixed(spec)
    if cal.method == VolMethod.ROLLING_REALIZED:
        return _rolling(spec, source, horizon_s)
    if cal.method == VolMethod.HYBRID:
        if horizon_s < cal.hybrid_threshold_s:
            return _rolling(spec, source, horizon_s)
        return _fixed(spec)
    raise ValueError(f"unknown VolMethod: {cal.method!r}")

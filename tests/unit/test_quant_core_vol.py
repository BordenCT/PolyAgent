from dataclasses import dataclass

import pytest

from polyagent.services.quant.core.vol import (
    VolCalibration, VolMethod, compute_vol,
)


class _FakeSource:
    def __init__(self, rolling_value: float):
        self._v = rolling_value
        self.last_window_s: int | None = None
    def realized_vol(self, window_s: int) -> float:
        self.last_window_s = window_s
        return self._v


@dataclass
class _Spec:
    default_vol: float
    vol_calibration: VolCalibration


def test_compute_vol_fixed_returns_fixed_value():
    spec = _Spec(default_vol=0.6, vol_calibration=VolCalibration(method=VolMethod.FIXED, fixed_value=0.42))
    assert compute_vol(spec, _FakeSource(0.0), horizon_s=300.0) == 0.42


def test_compute_vol_fixed_falls_back_to_default_when_unset():
    spec = _Spec(default_vol=0.6, vol_calibration=VolCalibration(method=VolMethod.FIXED))
    assert compute_vol(spec, _FakeSource(0.0), horizon_s=300.0) == 0.6


def test_compute_vol_rolling_uses_clamped_lookback():
    spec = _Spec(default_vol=0.6, vol_calibration=VolCalibration(
        method=VolMethod.ROLLING_REALIZED,
        rolling_min_s=300, rolling_max_s=86400, rolling_horizon_multiplier=4.0,
    ))
    src = _FakeSource(0.5)
    # 60 * 4 = 240 < min 300, clamped up to 300
    assert compute_vol(spec, src, horizon_s=60.0) == 0.5
    assert src.last_window_s == 300
    # 1h * 4 = 4h = 14400, in range
    src2 = _FakeSource(0.5)
    compute_vol(spec, src2, horizon_s=3600.0)
    assert src2.last_window_s == 14400
    # 30d * 4 = much more than 86400, clamped down to 86400
    src3 = _FakeSource(0.5)
    compute_vol(spec, src3, horizon_s=30 * 86400.0)
    assert src3.last_window_s == 86400


def test_compute_vol_rolling_falls_back_to_default_when_zero():
    spec = _Spec(default_vol=0.6, vol_calibration=VolCalibration(
        method=VolMethod.ROLLING_REALIZED,
    ))
    assert compute_vol(spec, _FakeSource(0.0), horizon_s=300.0) == 0.6


def test_compute_vol_hybrid_short_horizon_uses_rolling():
    spec = _Spec(default_vol=0.6, vol_calibration=VolCalibration(
        method=VolMethod.HYBRID, fixed_value=0.99, hybrid_threshold_s=14400,
    ))
    assert compute_vol(spec, _FakeSource(0.4), horizon_s=300.0) == 0.4


def test_compute_vol_hybrid_long_horizon_uses_fixed():
    spec = _Spec(default_vol=0.6, vol_calibration=VolCalibration(
        method=VolMethod.HYBRID, fixed_value=0.99, hybrid_threshold_s=14400,
    ))
    assert compute_vol(spec, _FakeSource(0.4), horizon_s=86400.0) == 0.99

"""Unit tests for the tick-history feature primitives."""
from __future__ import annotations

import math

import numpy as np
import pytest

from polyagent.services.quant.ml.features import bns_jump_ratio, vpin


# ---- VPIN -----------------------------------------------------------------

def test_vpin_balanced_flow_is_near_zero():
    """Perfectly alternating buy/sell with equal size = bucket-balanced ⇒ VPIN ≈ 0."""
    n = 1000
    sides = np.tile([1, -1], n // 2).astype(float)
    sizes = np.ones(n)
    v = vpin(sides, sizes, n_buckets=50)
    assert v < 0.1, f"balanced flow should produce low VPIN, got {v}"


def test_vpin_all_one_sided_is_one():
    """All buys ⇒ each bucket is fully signed ⇒ VPIN = 1.0."""
    sides = np.ones(1000)
    sizes = np.ones(1000)
    assert vpin(sides, sizes, n_buckets=50) == pytest.approx(1.0, abs=1e-6)


def test_vpin_returns_nan_when_insufficient_trades():
    sides = np.ones(10)
    sizes = np.ones(10)
    assert math.isnan(vpin(sides, sizes, n_buckets=50))


def test_vpin_handles_zero_size():
    """If total volume is zero, VPIN is NaN, not a crash."""
    sides = np.array([1, -1])
    sizes = np.zeros(2)
    assert math.isnan(vpin(sides, sizes, n_buckets=2))


def test_vpin_handles_uneven_buckets():
    """With varying trade sizes, bucketing still distributes total volume."""
    rng = np.random.default_rng(0)
    n = 5000
    sides = rng.choice([-1.0, 1.0], size=n)
    sizes = rng.exponential(1.0, size=n)
    v = vpin(sides, sizes, n_buckets=50)
    assert 0.0 <= v <= 1.0


# ---- BNS jump ratio --------------------------------------------------------

def test_jump_ratio_returns_nan_for_short_series():
    assert math.isnan(bns_jump_ratio(np.array([])))
    assert math.isnan(bns_jump_ratio(np.array([0.01, 0.02])))


def test_jump_ratio_pure_diffusion_is_near_zero():
    """No jumps -> RV ≈ BV -> ratio ≈ 0 (Barndorff-Nielsen/Shephard)."""
    rng = np.random.default_rng(42)
    returns = rng.normal(0.0, 0.001, size=2000)
    ratio = bns_jump_ratio(returns)
    # Expected close to zero; finite-sample noise allows ±0.15.
    assert abs(ratio) < 0.20, f"diffusion ratio should be near 0, got {ratio}"


def test_jump_ratio_with_inserted_jumps_is_positive():
    """Inject discrete jumps; the BNS ratio should rise materially."""
    rng = np.random.default_rng(7)
    returns = rng.normal(0.0, 0.001, size=2000)
    # Insert 5 large jumps at well-separated positions.
    for i in (100, 500, 1000, 1500, 1800):
        returns[i] = 0.05  # 5% in a single step
    ratio = bns_jump_ratio(returns)
    assert ratio > 0.30, f"with jumps, ratio should rise; got {ratio}"


def test_jump_ratio_handles_all_zero_returns():
    """All zero returns -> RV = 0 -> formula short-circuits to 0."""
    assert bns_jump_ratio(np.zeros(100)) == 0.0

"""Tests for the BTC 5m lognormal estimator."""
from __future__ import annotations
from decimal import Decimal

import pytest

from polyagent.services.btc5m.estimator import estimate_up_probability


def test_atm_near_expiration_is_half():
    # At the money, any positive TTM: p_up ≈ 0.5 (slight drift, but σ²/2 term small)
    p = estimate_up_probability(
        start_price=Decimal("65000"),
        current_spot=Decimal("65000"),
        seconds_to_resolution=60,
        annualised_vol=0.50,
    )
    assert 0.48 < p < 0.52


def test_deep_itm_is_near_one():
    # Current spot 5% above start, 60s TTM: very likely up
    p = estimate_up_probability(
        start_price=Decimal("65000"),
        current_spot=Decimal("68000"),
        seconds_to_resolution=60,
        annualised_vol=0.50,
    )
    assert p > 0.95


def test_deep_otm_is_near_zero():
    p = estimate_up_probability(
        start_price=Decimal("65000"),
        current_spot=Decimal("62000"),
        seconds_to_resolution=60,
        annualised_vol=0.50,
    )
    assert p < 0.05


def test_higher_vol_pulls_otm_toward_half():
    low = estimate_up_probability(
        Decimal("65000"), Decimal("64800"), 300, annualised_vol=0.20
    )
    high = estimate_up_probability(
        Decimal("65000"), Decimal("64800"), 300, annualised_vol=1.50
    )
    # Higher vol → more uncertainty → closer to 0.5
    assert abs(high - 0.5) < abs(low - 0.5)


def test_zero_vol_returns_half_when_flat():
    # Edge case: zero vol, ATM — should not raise, returns ~0.5
    p = estimate_up_probability(
        start_price=Decimal("65000"),
        current_spot=Decimal("65000"),
        seconds_to_resolution=60,
        annualised_vol=0.0,
    )
    assert p == pytest.approx(0.5, abs=0.01)


def test_zero_ttm_is_binary():
    # TTM = 0 should collapse: p_up ≈ 1 if current > start, ≈ 0 if lower
    p_above = estimate_up_probability(Decimal("65000"), Decimal("65100"), 0.0, 0.50)
    p_below = estimate_up_probability(Decimal("65000"), Decimal("64900"), 0.0, 0.50)
    assert p_above > 0.99
    assert p_below < 0.01


def test_output_always_in_unit_interval():
    # Adversarial: extreme inputs shouldn't escape [0, 1]
    for params in [
        (Decimal("65000"), Decimal("130000"), 10, 3.0),
        (Decimal("65000"), Decimal("100"), 1, 5.0),
        (Decimal("65000"), Decimal("65000"), 10000, 0.001),
    ]:
        p = estimate_up_probability(*params)
        assert 0.0 <= p <= 1.0, f"out of range for {params}: {p}"

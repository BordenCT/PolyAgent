"""Estimator tests covering UP / DOWN / RANGE plus the confidence rule."""
from decimal import Decimal

from polyagent.services.crypto_quant.estimator import estimate_yes_probability
from polyagent.services.crypto_quant.parser import CryptoStrike, StrikeKind


def _strike_up(k: str) -> CryptoStrike:
    return CryptoStrike(asset="BTC", kind=StrikeKind.UP, strike=Decimal(k))


def _strike_down(k: str) -> CryptoStrike:
    return CryptoStrike(asset="BTC", kind=StrikeKind.DOWN, strike=Decimal(k))


def _strike_range(low: str, high: str) -> CryptoStrike:
    return CryptoStrike(
        asset="BTC", kind=StrikeKind.RANGE,
        strike=Decimal(low), upper_strike=Decimal(high),
    )


def test_up_atm_short_horizon_near_50pct():
    # Spot at strike, one day out, 60% σ — should be near 0.5 (slight downward
    # drift from the −σ²/2 term).
    r = estimate_yes_probability(
        _strike_up("80000"), Decimal("80000"), 0.60, hours_to_resolution=24,
    )
    assert 0.45 < r.probability < 0.51
    assert r.confidence == 0.95


def test_up_far_otm_short_horizon_near_zero():
    # BTC at $74k, strike $80k, one day out, 60% σ — far OTM, P(YES) tiny.
    # Distance is ~2.5σ — still inside the high-confidence band.
    r = estimate_yes_probability(
        _strike_up("80000"), Decimal("74000"), 0.60, hours_to_resolution=24,
    )
    assert r.probability < 0.05
    assert r.confidence == 0.95


def test_up_extremely_far_strike_drops_confidence():
    # BTC at $74k, strike $120k, one day out, 60% σ — well beyond 3σ.
    r = estimate_yes_probability(
        _strike_up("120000"), Decimal("74000"), 0.60, hours_to_resolution=24,
    )
    assert r.sigma_distance > 3
    assert r.confidence == 0.70


def test_down_is_complement_of_up():
    spot = Decimal("80000")
    up = estimate_yes_probability(_strike_up("80000"), spot, 0.60, 24)
    dn = estimate_yes_probability(_strike_down("80000"), spot, 0.60, 24)
    assert abs((up.probability + dn.probability) - 1.0) < 1e-9


def test_range_is_diff_of_above_strikes():
    spot = Decimal("75000")
    rng = estimate_yes_probability(_strike_range("74000", "76000"), spot, 0.60, 24)
    above_low = estimate_yes_probability(_strike_up("74000"), spot, 0.60, 24)
    above_high = estimate_yes_probability(_strike_up("76000"), spot, 0.60, 24)
    assert abs(rng.probability - (above_low.probability - above_high.probability)) < 1e-9


def test_long_horizon_drops_confidence():
    # 60d horizon → confidence should drop even if strike is near spot.
    r = estimate_yes_probability(
        _strike_up("80000"), Decimal("80000"), 0.60, hours_to_resolution=60 * 24,
    )
    assert r.confidence == 0.70


def test_resolved_window_returns_binary():
    # 0 hours left and spot above strike = guaranteed YES, vol irrelevant.
    r = estimate_yes_probability(
        _strike_up("80000"), Decimal("85000"), 0.60, hours_to_resolution=0,
    )
    assert r.probability == 1.0


def test_probability_is_clamped_to_unit_interval():
    r = estimate_yes_probability(
        _strike_up("80000"), Decimal("1_000_000"), 0.60, hours_to_resolution=1,
    )
    assert 0.0 <= r.probability <= 1.0

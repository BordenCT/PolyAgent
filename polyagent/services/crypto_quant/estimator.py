"""Map a parsed CryptoStrike + spot + vol → (P(YES), confidence).

Reuses the lognormal Φ(d₂) primitive in btc5m/estimator.py — the math is
horizon-agnostic, so the same function serves 5m bets and 30d strikes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from polyagent.services.btc5m.estimator import estimate_up_probability
from polyagent.services.crypto_quant.parser import CryptoStrike, StrikeKind

_SECONDS_PER_YEAR = 365.25 * 24 * 3600


@dataclass(frozen=True)
class QuantResult:
    probability: float          # P(YES) in [0, 1]
    confidence: float           # 0.95 if short-dated and not far-strike, else 0.7
    sigma_distance: float       # |ln(S/K)| / (σ √T) for the primary strike


def estimate_yes_probability(
    strike: CryptoStrike,
    current_spot: Decimal,
    annualised_vol: float,
    hours_to_resolution: float,
) -> QuantResult:
    """Compute P(YES) for a parsed strike using Black-Scholes Φ(d₂)."""
    seconds = max(0.0, hours_to_resolution * 3600.0)

    p_above_low = estimate_up_probability(
        strike.strike, current_spot, seconds, annualised_vol
    )

    if strike.kind == StrikeKind.UP:
        p = p_above_low
    elif strike.kind == StrikeKind.DOWN:
        p = 1.0 - p_above_low
    else:  # RANGE
        assert strike.upper_strike is not None
        p_above_high = estimate_up_probability(
            strike.upper_strike, current_spot, seconds, annualised_vol
        )
        p = max(0.0, p_above_low - p_above_high)

    days = hours_to_resolution / 24.0
    T = seconds / _SECONDS_PER_YEAR
    sigma_distance = 0.0
    if T > 0 and annualised_vol > 0 and float(strike.strike) > 0 and float(current_spot) > 0:
        sigma_distance = abs(
            math.log(float(current_spot) / float(strike.strike))
        ) / (annualised_vol * math.sqrt(T))

    confidence = 0.95 if (days < 30 and sigma_distance < 3) else 0.70
    return QuantResult(probability=p, confidence=confidence, sigma_distance=sigma_distance)

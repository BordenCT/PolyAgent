"""Lognormal P(up) estimator for binary horizon markets.

Pure function. No I/O. Never raises. Parameterized on time-to-maturity so
the same code serves 5m bets and 30d strikes across BTC, ETH, FX, etc.
"""
from __future__ import annotations

import math
from decimal import Decimal

_SECONDS_PER_YEAR = 365.25 * 24 * 3600


def estimate_up_probability(
    start_price: Decimal,
    current_spot: Decimal,
    seconds_to_resolution: float,
    annualised_vol: float,
) -> float:
    """P(spot_at_resolution >= start_price) under lognormal dynamics.

    Black-Scholes Φ(d₂) with r = 0 (fair game over short horizons):

        d₂ = (ln(S/K) - σ² T / 2) / (σ √T)
        P(up) = Φ(d₂)

    Args:
        start_price: The strike (K) — price at window_start_ts, or the
                     current spot before the window opens.
        current_spot: Current BTC spot price from Coinbase.
        seconds_to_resolution: Time until window closes. <= 0 means the
                               market is effectively resolved.
        annualised_vol: σ in fractional units (e.g., 0.50 for 50%/year).

    Returns:
        P(up) clamped to [0, 1].
    """
    S = float(current_spot)
    K = float(start_price)

    if seconds_to_resolution <= 0:
        # Window closed: outcome is determined by current spot vs start
        if S > K:
            return 1.0
        if S < K:
            return 0.0
        return 0.5

    if annualised_vol <= 0 or K <= 0 or S <= 0:
        # Degenerate: no randomness left. Hand back 0.5 at ATM, else binary.
        if S > K:
            return 1.0
        if S < K:
            return 0.0
        return 0.5

    T = seconds_to_resolution / _SECONDS_PER_YEAR
    vol_sqrt_t = annualised_vol * math.sqrt(T)
    # d2 per Black-Scholes with r=0
    d2 = (math.log(S / K) - 0.5 * annualised_vol * annualised_vol * T) / vol_sqrt_t

    # Standard normal CDF via erf
    p = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
    if p < 0.0:
        return 0.0
    if p > 1.0:
        return 1.0
    return p

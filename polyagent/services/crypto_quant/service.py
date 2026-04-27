"""CryptoQuantService — the integration seam between the brain and the
deterministic Φ(d₂) estimator.

When a market matches a known crypto-strike pattern, this service is consulted
in place of the LLM. It owns the spot sources for BTC and ETH, the assumed
volatilities, and produces the same Thesis-shaped output the LLM would.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from polyagent.services.btc5m.spot import CoinbaseSpotSource
from polyagent.services.crypto_quant.estimator import (
    QuantResult,
    estimate_yes_probability,
)
from polyagent.services.crypto_quant.parser import (
    CryptoStrike,
    StrikeKind,
    parse_question,
)

logger = logging.getLogger("polyagent.services.crypto_quant")


class CryptoQuantService:
    """Quant pipeline for crypto-strike markets.

    Args:
        btc_spot: Spot source for BTC-USD. Must be tick()'d periodically.
        eth_spot: Spot source for ETH-USD. Must be tick()'d periodically.
        btc_vol: Default annualised σ for BTC (e.g., 0.60).
        eth_vol: Default annualised σ for ETH (e.g., 0.75).
    """

    def __init__(
        self,
        btc_spot: CoinbaseSpotSource,
        eth_spot: CoinbaseSpotSource,
        btc_vol: float = 0.60,
        eth_vol: float = 0.75,
    ) -> None:
        self._btc = btc_spot
        self._eth = eth_spot
        self._vol = {"BTC": btc_vol, "ETH": eth_vol}

    def matches(self, question: str) -> CryptoStrike | None:
        return parse_question(question)

    def evaluate(
        self, question: str, hours_to_resolution: float
    ) -> tuple[CryptoStrike, QuantResult, str] | None:
        """Return (parsed strike, quant result, thesis text) or None if not handled.

        Returns None when the question doesn't match a supported pattern, or
        when the spot source has no current price (haven't ticked yet).
        """
        strike = self.matches(question)
        if strike is None:
            return None

        spot_source = self._btc if strike.asset == "BTC" else self._eth
        spot = spot_source.current()
        if spot is None or spot <= 0:
            logger.warning(
                "crypto_quant: no spot price yet for %s, skipping %r",
                strike.asset,
                question,
            )
            return None

        vol = self._vol[strike.asset]
        result = estimate_yes_probability(strike, spot, vol, hours_to_resolution)
        thesis_text = self._build_thesis_text(strike, Decimal(str(spot)), vol, result)
        return strike, result, thesis_text

    @staticmethod
    def _build_thesis_text(
        strike: CryptoStrike, spot: Decimal, vol: float, result: QuantResult
    ) -> str:
        kind_desc = {
            StrikeKind.UP: f"spot >= ${strike.strike:,}",
            StrikeKind.DOWN: f"spot < ${strike.strike:,}",
            StrikeKind.RANGE: f"${strike.strike:,} <= spot < ${strike.upper_strike:,}",
        }[strike.kind]
        return (
            f"[crypto_quant] {strike.asset} {kind_desc} | "
            f"current_spot=${spot:,.2f} sigma={vol:.2f} "
            f"sigma_distance={result.sigma_distance:.2f} "
            f"P(YES)={result.probability:.4f} confidence={result.confidence:.2f} "
            f"(closed-form Φ(d₂), not LLM)"
        )

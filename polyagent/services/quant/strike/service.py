"""Brain integration seam for strike-market questions.

Replaces CryptoQuantService with identical (matches/evaluate) surface.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from decimal import Decimal

from polyagent.services.quant.assets.registry import get
from polyagent.services.quant.assets.spec import MarketFamily
from polyagent.services.quant.assets.sources.base import PriceSource
from polyagent.services.quant.core.estimator import estimate_up_probability
from polyagent.services.quant.core.vol import compute_vol
from polyagent.services.quant.strike.parser import (
    ParsedStrike, StrikeKind, parse_question,
)

logger = logging.getLogger("polyagent.services.quant.strike")

_SECONDS_PER_YEAR = 365.25 * 24 * 3600


@dataclass(frozen=True)
class QuantResult:
    probability: float
    confidence: float
    sigma_distance: float


def _evaluate_strike(
    parsed: ParsedStrike, spot: Decimal, vol: float, hours_to_resolution: float,
) -> QuantResult:
    seconds = max(0.0, hours_to_resolution * 3600.0)
    p_above_low = estimate_up_probability(
        parsed.strike, spot, seconds, vol,
    )
    if parsed.kind == StrikeKind.UP:
        p = p_above_low
    elif parsed.kind == StrikeKind.DOWN:
        p = 1.0 - p_above_low
    else:
        assert parsed.upper_strike is not None
        p_above_high = estimate_up_probability(
            parsed.upper_strike, spot, seconds, vol,
        )
        p = max(0.0, p_above_low - p_above_high)
    days = hours_to_resolution / 24.0
    T = seconds / _SECONDS_PER_YEAR
    sigma_distance = 0.0
    if T > 0 and vol > 0 and float(parsed.strike) > 0 and float(spot) > 0:
        sigma_distance = abs(math.log(float(spot) / float(parsed.strike))) / (vol * math.sqrt(T))
    confidence = 0.95 if (days < 30 and sigma_distance < 3) else 0.70
    return QuantResult(probability=p, confidence=confidence, sigma_distance=sigma_distance)


def _build_thesis_text(
    parsed: ParsedStrike, spot: Decimal, vol: float, result: QuantResult,
) -> str:
    if parsed.kind == StrikeKind.UP:
        kind_desc = f"spot >= ${parsed.strike:,}"
    elif parsed.kind == StrikeKind.DOWN:
        kind_desc = f"spot < ${parsed.strike:,}"
    else:
        kind_desc = f"${parsed.strike:,} <= spot < ${parsed.upper_strike:,}"
    return (
        f"[quant_strike] {parsed.asset_id} {kind_desc} | "
        f"current_spot=${spot:,.2f} sigma={vol:.2f} "
        f"sigma_distance={result.sigma_distance:.2f} "
        f"P(YES)={result.probability:.4f} confidence={result.confidence:.2f} "
        f"(closed-form Phi(d2), not LLM)"
    )


class QuantStrikeService:
    """Brain integration seam: replaces CryptoQuantService.

    Args:
        sources: dict mapping asset_id to PriceSource. The orchestrator
            owns the source instances; this service reads from them.
    """

    def __init__(self, sources: dict[str, PriceSource]) -> None:
        self._sources = sources

    def matches(self, question: str) -> ParsedStrike | None:
        return parse_question(question)

    def evaluate(
        self, question: str, hours_to_resolution: float
    ) -> tuple[ParsedStrike, QuantResult, str] | None:
        parsed = self.matches(question)
        if parsed is None:
            return None
        spec = get(parsed.asset_id)
        if spec is None or MarketFamily.STRIKE not in spec.supported_market_families:
            return None
        source = self._sources.get(parsed.asset_id)
        if source is None:
            logger.warning("quant_strike: no source for %s", parsed.asset_id)
            return None
        spot = source.current()
        if spot is None or spot <= 0:
            logger.warning("quant_strike: no spot for %s", parsed.asset_id)
            return None
        vol = compute_vol(spec, source, horizon_s=hours_to_resolution * 3600.0)
        result = _evaluate_strike(parsed, spot, vol, hours_to_resolution)
        thesis = _build_thesis_text(parsed, spot, vol, result)
        return parsed, result, thesis

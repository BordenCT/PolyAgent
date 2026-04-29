"""Registry-driven question parser for strike markets.

Iterates registry.enabled_for(STRIKE), tries each asset's
question_keywords against the standard above/below/between patterns.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from polyagent.services.quant.assets.registry import enabled_for
from polyagent.services.quant.assets.spec import MarketFamily


class StrikeKind(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    RANGE = "RANGE"


@dataclass(frozen=True)
class ParsedStrike:
    asset_id: str
    kind: StrikeKind
    strike: Decimal
    upper_strike: Decimal | None = None


_NUM = r"\$([\d,]+(?:\.\d+)?)"


def _to_decimal(raw: str) -> Decimal:
    return Decimal(raw.replace(",", ""))


def _build_patterns_for_keyword(kw: str):
    """Return (RE_ABOVE, RE_BELOW, RE_BETWEEN) for a single keyword."""
    kw_re = re.escape(kw)
    above = re.compile(rf"\bWill the price of {kw_re} be above {_NUM}\b", re.IGNORECASE)
    below = re.compile(rf"\bWill the price of {kw_re} be below {_NUM}\b", re.IGNORECASE)
    between = re.compile(
        rf"\bWill the price of {kw_re} be between {_NUM} and {_NUM}\b", re.IGNORECASE,
    )
    return above, below, between


def parse_question(question: str) -> ParsedStrike | None:
    """Return a ParsedStrike for supported patterns, or None."""
    if not question:
        return None
    for spec in enabled_for(MarketFamily.STRIKE):
        for kw in spec.question_keywords:
            above_re, below_re, between_re = _build_patterns_for_keyword(kw)
            if (m := between_re.search(question)):
                low, high = _to_decimal(m.group(1)), _to_decimal(m.group(2))
                if low > high:
                    low, high = high, low
                return ParsedStrike(
                    asset_id=spec.asset_id, kind=StrikeKind.RANGE,
                    strike=low, upper_strike=high,
                )
            if (m := above_re.search(question)):
                return ParsedStrike(
                    asset_id=spec.asset_id, kind=StrikeKind.UP,
                    strike=_to_decimal(m.group(1)),
                )
            if (m := below_re.search(question)):
                return ParsedStrike(
                    asset_id=spec.asset_id, kind=StrikeKind.DOWN,
                    strike=_to_decimal(m.group(1)),
                )
    return None

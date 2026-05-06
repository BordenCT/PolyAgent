"""Registry-driven question parser for strike markets.

Iterates registry.enabled_for(STRIKE), tries each asset's
question_keywords against the standard above/below/between patterns.

In addition to the strike kind and value, the parser optionally captures
the resolution-date suffix ("on <date>") so callers can group correlated
markets that resolve on the same settlement event. The cluster_key()
helper packages this into a hashable (asset_id, date) tuple used by the
executor's correlation cap.
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
    resolution_date: str | None = None


_NUM = r"\$([\d,]+(?:\.\d+)?)"
_DATE_TAIL = r"(?:\s+on\s+(?P<date>.+?))?"


def _to_decimal(raw: str) -> Decimal:
    return Decimal(raw.replace(",", ""))


def _strip_date(raw: str | None) -> str | None:
    """Return the ``on <date>`` capture trimmed of whitespace and trailing ``?``.

    Casing is preserved so callers can show a human-readable date. The
    cluster key normalises separately (see ``cluster_key``) so two
    questions written with different capitalisation still collide in the
    correlation cap.
    """
    if raw is None:
        return None
    cleaned = raw.strip().rstrip("?").strip()
    return cleaned or None


def _build_patterns_for_keyword(kw: str):
    """Return (RE_ABOVE, RE_BELOW, RE_BETWEEN) for a single keyword.

    Each pattern optionally captures a trailing ``on <date>`` clause into
    a named ``date`` group. The date is greedy up to a trailing ``?`` or
    end-of-string and is normalized (lowercased, stripped) by the caller
    so cluster keys collide regardless of question casing.
    """
    kw_re = re.escape(kw)
    above = re.compile(
        rf"\bWill the price of {kw_re} be above {_NUM}{_DATE_TAIL}\??$",
        re.IGNORECASE,
    )
    below = re.compile(
        rf"\bWill the price of {kw_re} be below {_NUM}{_DATE_TAIL}\??$",
        re.IGNORECASE,
    )
    between = re.compile(
        rf"\bWill the price of {kw_re} be between {_NUM} and {_NUM}{_DATE_TAIL}\??$",
        re.IGNORECASE,
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
                    resolution_date=_strip_date(m.groupdict().get("date")),
                )
            if (m := above_re.search(question)):
                return ParsedStrike(
                    asset_id=spec.asset_id, kind=StrikeKind.UP,
                    strike=_to_decimal(m.group(1)),
                    resolution_date=_strip_date(m.groupdict().get("date")),
                )
            if (m := below_re.search(question)):
                return ParsedStrike(
                    asset_id=spec.asset_id, kind=StrikeKind.DOWN,
                    strike=_to_decimal(m.group(1)),
                    resolution_date=_strip_date(m.groupdict().get("date")),
                )
    return None


def cluster_key(question: str) -> tuple[str, str] | None:
    """Return (asset_id, resolution_date) for a strike question, else None.

    Used by the executor's correlation cap to group positions that resolve
    on the same settlement event (same asset, same date). Returns None
    when the question is not a recognised strike pattern OR when the
    resolution date is missing, since both cases are uncorrelated for
    cap purposes.
    """
    parsed = parse_question(question)
    if parsed is None or parsed.resolution_date is None:
        return None
    return (parsed.asset_id, parsed.resolution_date.lower())

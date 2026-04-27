"""Parse Polymarket crypto-strike questions into structured strikes."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class StrikeKind(str, Enum):
    UP = "UP"           # YES if spot >= strike at expiry
    DOWN = "DOWN"       # YES if spot < strike at expiry
    RANGE = "RANGE"     # YES if low <= spot < high at expiry


@dataclass(frozen=True)
class CryptoStrike:
    asset: str          # "BTC" | "ETH"
    kind: StrikeKind
    strike: Decimal     # primary strike (low bound for RANGE)
    upper_strike: Decimal | None = None   # set only for RANGE


_ASSET_MAP = {
    "bitcoin": "BTC",
    "btc": "BTC",
    "ethereum": "ETH",
    "eth": "ETH",
}

_NUM = r"\$([\d,]+(?:\.\d+)?)"

_RE_ABOVE = re.compile(
    rf"\bWill the price of (Bitcoin|Ethereum|BTC|ETH) be above {_NUM}\b",
    re.IGNORECASE,
)
_RE_BELOW = re.compile(
    rf"\bWill the price of (Bitcoin|Ethereum|BTC|ETH) be below {_NUM}\b",
    re.IGNORECASE,
)
_RE_BETWEEN = re.compile(
    rf"\bWill the price of (Bitcoin|Ethereum|BTC|ETH) be between {_NUM} and {_NUM}\b",
    re.IGNORECASE,
)


def _to_decimal(raw: str) -> Decimal:
    return Decimal(raw.replace(",", ""))


def parse_question(question: str) -> CryptoStrike | None:
    """Return a CryptoStrike for supported patterns, or None.

    Supported (case-insensitive):
        "Will the price of <Bitcoin|Ethereum> be above $K on ..."
        "Will the price of <Bitcoin|Ethereum> be below $K on ..."
        "Will the price of <Bitcoin|Ethereum> be between $K1 and $K2 on ..."
    """
    if not question:
        return None

    m = _RE_BETWEEN.search(question)
    if m:
        asset = _ASSET_MAP[m.group(1).lower()]
        low, high = _to_decimal(m.group(2)), _to_decimal(m.group(3))
        if low > high:
            low, high = high, low
        return CryptoStrike(asset=asset, kind=StrikeKind.RANGE, strike=low, upper_strike=high)

    m = _RE_ABOVE.search(question)
    if m:
        asset = _ASSET_MAP[m.group(1).lower()]
        return CryptoStrike(asset=asset, kind=StrikeKind.UP, strike=_to_decimal(m.group(2)))

    m = _RE_BELOW.search(question)
    if m:
        asset = _ASSET_MAP[m.group(1).lower()]
        return CryptoStrike(asset=asset, kind=StrikeKind.DOWN, strike=_to_decimal(m.group(2)))

    return None

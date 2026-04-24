"""Coarse market classifier — tags each market with a MarketClass.

Pure function with no I/O, no side effects, never raises. The rule list is
ordered; the first class whose pattern list matches the question or category
wins. Default is MarketClass.OTHER.
"""
from __future__ import annotations

import re

from polyagent.models import MarketClass


# Patterns are compiled with re.IGNORECASE below. Word boundaries (\b) are
# used where the token could otherwise match inside another word.
CRYPTO_PATTERNS = [
    r"\bbitcoin\b",
    r"\bbtc\b",
    r"\bethereum\b",
    r"\beth\b",
    r"\bsolana\b",
    r"\bsol\b",
    r"\bxrp\b",
    r"\bdogecoin\b",
    r"\bdoge\b",
    r"\bcrypto\b",
    r"\bstablecoin\b",
    r"\busdc\b",
    r"\busdt\b",
]

SPORTS_PATTERNS = [
    r" vs\. ",
    r" vs ",
    r" Open:",
    r"\bBO3\b",
    r"\bBO5\b",
    r"Spread:",
    r"\bMLB\b",
    r"\bNBA\b",
    r"\bNHL\b",
    r"\bNFL\b",
    r"\bNCAA[MF]?\b",
    r"\bUEFA\b",
    r"\bLCK\b",
    r"\bLoL\b",
    r"\bDota\b",
    r"\bCS2\b",
    r"\bValorant\b",
    r"Premier League",
    r"La Liga",
    r"Bundesliga",
    r"Serie A",
    r"^Will .+ win on \d{4}-\d{2}-\d{2}\??$",
]

POLITICS_PATTERNS = [
    r"\bpresident(ial)?\b",
    r"\belection\b",
    r"\bprimary\b",
    r"\bSenate\b",
    r"\bCongress\b",
    r"Supreme Court",
    r"\bTrump\b",
    r"\bBiden\b",
    r"\bHarris\b",
    r"\bVance\b",
    r"\bgovernor\b",
    r"\bimpeach(ment)?\b",
    r"\bcabinet\b",
]

MACRO_PATTERNS = [
    r"\bCPI\b",
    r"\binflation\b",
    r"\bFed\b",
    r"\bFOMC\b",
    r"interest rate",
    r"\brecession\b",
    r"\bGDP\b",
    r"\bunemployment\b",
    r"jobs report",
    r"\bpayrolls?\b",
]


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


CLASS_RULES: list[tuple[MarketClass, list[re.Pattern[str]]]] = [
    (MarketClass.CRYPTO, _compile(CRYPTO_PATTERNS)),
    (MarketClass.SPORTS, _compile(SPORTS_PATTERNS)),
    (MarketClass.POLITICS, _compile(POLITICS_PATTERNS)),
    (MarketClass.MACRO, _compile(MACRO_PATTERNS)),
]

# Polymarket category string -> MarketClass (case-insensitive match).
# If Gamma tags a market definitively we trust it over the question scan,
# but only for classes where the mapping is unambiguous.
CATEGORY_MAP: dict[str, MarketClass] = {
    "sports": MarketClass.SPORTS,
    "politics": MarketClass.POLITICS,
    "crypto": MarketClass.CRYPTO,
}


def classify(question: str, category: str) -> MarketClass:
    """Return the coarse class of a market. Pure, never raises.

    Order of evaluation:
      1. Polymarket's own `category` tag (if it's one we trust).
      2. The question-text rule cascade.
      3. OTHER.

    Args:
        question: The market question text (may be empty).
        category: The Polymarket category string (may be empty or 'unknown').

    Returns:
        The first MarketClass whose rules match, or MarketClass.OTHER.
    """
    cat_norm = (category or "").strip().lower()
    if cat_norm in CATEGORY_MAP:
        return CATEGORY_MAP[cat_norm]

    q = question or ""
    for cls, patterns in CLASS_RULES:
        for p in patterns:
            if p.search(q):
                return cls

    return MarketClass.OTHER

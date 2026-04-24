"""Coarse market classifier — tags each market with a MarketClass.

Pure function with no I/O, no side effects, never raises. The rule list is
ordered; the first class whose pattern list matches the question or category
wins. Default is MarketClass.OTHER.

One special case: if the question contains a known sports-event term alongside
a politics keyword, SPORTS takes priority (e.g. "Will Biden attend the Super
Bowl?" trades as a sports market, not a political one). Sports-event terms
alone — without any other recognisable signal — fall through to OTHER so that
celebrity-adjacent questions don't get mis-tagged (e.g. Taylor Swift at the
Super Bowl is not a sports market we trade).
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

# Sports-event terms that are too culturally broad to classify a market as
# SPORTS on their own (a celebrity can "attend the Super Bowl" without it
# being a tradeable sports market). These only upgrade a POLITICS match to
# SPORTS — they do not trigger SPORTS classification independently.
SPORTS_EVENT_PATTERNS = [
    r"Super Bowl",
    r"World Series",
    r"Stanley Cup",
    r"\bNBA Finals\b",
    r"Champions League Final",
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

_SPORTS_EVENT_RE: list[re.Pattern[str]] = _compile(SPORTS_EVENT_PATTERNS)


# Polymarket category string -> MarketClass (case-insensitive match).
# If Gamma tags a market definitively we trust it over the question scan,
# but only for classes where the mapping is unambiguous.
CATEGORY_MAP: dict[str, MarketClass] = {
    "sports": MarketClass.SPORTS,
    "politics": MarketClass.POLITICS,
    "crypto": MarketClass.CRYPTO,
}


def _matches_any(patterns: list[re.Pattern[str]], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def classify(question: str, category: str) -> MarketClass:
    """Return the coarse class of a market. Pure, never raises.

    Order of evaluation:
      1. Polymarket's own ``category`` tag (if it's one we trust).
      2. The question-text rule cascade (CRYPTO → SPORTS → POLITICS → MACRO).
      3. POLITICS + sports-event override: if POLITICS fired but the question
         also contains a known sports-event term, reclassify as SPORTS.
      4. OTHER.

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
    matched: MarketClass | None = None
    for cls, patterns in CLASS_RULES:
        if _matches_any(patterns, q):
            matched = cls
            break

    if matched is MarketClass.POLITICS and _matches_any(_SPORTS_EVENT_RE, q):
        return MarketClass.SPORTS

    return matched if matched is not None else MarketClass.OTHER

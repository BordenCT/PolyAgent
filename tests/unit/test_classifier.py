"""Tests for the market classifier."""
from __future__ import annotations

import pytest

from polyagent.models import MarketClass
from polyagent.services.classifier import classify


CRYPTO_QUESTIONS = [
    "Will Bitcoin reach $80,000 on April 22?",
    "Will ETH hit $5000 by May?",
    "Will Solana flip Ethereum in market cap this year?",
    "Will XRP be classified as a security in 2026?",
    "Will USDC maintain its peg through Q2?",
]

SPORTS_QUESTIONS = [
    "Madrid Open: Cristian Garin vs Alexander Blockx",
    "LoL: HANJIN BRION vs Dplus KIA (BO3) - LCK Rounds 1-2",
    "Chicago White Sox vs. Arizona Diamondbacks",
    "Ducks vs. Oilers",
    "Spread: FC Barcelona (-2.5)",
    "Will Burnley FC win on 2026-04-22?",
    "Dota 2: Xtreme Gaming vs Team Spirit (BO3) - PGL Wallachia Group Stage",
]

POLITICS_QUESTIONS = [
    "Will Trump be re-elected in 2028?",
    "Will the Senate pass the budget bill by June?",
    "Will Harris announce a presidential run?",
    "Will Congress impeach any sitting cabinet member in 2026?",
    "Who will win the 2026 Democratic primary in Ohio?",
]

MACRO_QUESTIONS = [
    "Will CPI YoY be above 3% in May?",
    "Will the Fed cut interest rates at the June FOMC meeting?",
    "Will US GDP growth exceed 2% in Q2 2026?",
    "Will unemployment hit 5% by year-end?",
    "Will the May jobs report show negative payrolls growth?",
]

OTHER_QUESTIONS = [
    "Will Taylor Swift attend the Super Bowl?",  # no sports keyword, should be OTHER
    "Will SpaceX launch Starship by Q3 2026?",
    "Will the new Marvel movie gross over $1B?",
    "",
    "   ",
]


@pytest.mark.parametrize("q", CRYPTO_QUESTIONS)
def test_classifies_crypto(q):
    assert classify(q, "unknown") == MarketClass.CRYPTO


@pytest.mark.parametrize("q", SPORTS_QUESTIONS)
def test_classifies_sports(q):
    assert classify(q, "unknown") == MarketClass.SPORTS


@pytest.mark.parametrize("q", POLITICS_QUESTIONS)
def test_classifies_politics(q):
    assert classify(q, "unknown") == MarketClass.POLITICS


@pytest.mark.parametrize("q", MACRO_QUESTIONS)
def test_classifies_macro(q):
    assert classify(q, "unknown") == MarketClass.MACRO


@pytest.mark.parametrize("q", OTHER_QUESTIONS)
def test_falls_back_to_other(q):
    assert classify(q, "unknown") == MarketClass.OTHER


def test_category_sports_overrides_question():
    # Ambiguous question, but Polymarket tagged it Sports -> trust category.
    assert classify("Will it happen?", "Sports") == MarketClass.SPORTS


def test_category_politics_overrides_question():
    assert classify("Will it happen?", "Politics") == MarketClass.POLITICS


def test_category_case_insensitive():
    assert classify("Will it happen?", "sports") == MarketClass.SPORTS
    assert classify("Will it happen?", "SPORTS") == MarketClass.SPORTS


def test_adversarial_tennessee_senate_is_politics_not_sports():
    # "Tennessee" superficially looks like a tennis keyword (it isn't, but we
    # want to be sure the Senate signal dominates).
    q = "Will the Tennessee Senate race be decided by September?"
    assert classify(q, "unknown") == MarketClass.POLITICS


def test_priority_crypto_beats_sports_keyword():
    # Adversarial: crypto question that happens to mention the NBA.
    q = "Will BTC sponsor the NBA Finals by July?"
    assert classify(q, "unknown") == MarketClass.CRYPTO


def test_priority_sports_beats_politics_keyword():
    # A market with both a politician name and a clear sports keyword (vs.)
    # — sports is checked before politics in the cascade, so it wins.
    q = "Will Trump attend Lakers vs. Celtics?"
    assert classify(q, "unknown") == MarketClass.SPORTS


def test_never_raises_on_weird_input():
    # Pathological inputs should fall through to OTHER, not blow up.
    assert classify("???", "") == MarketClass.OTHER
    assert classify("\n\t", "\n") == MarketClass.OTHER

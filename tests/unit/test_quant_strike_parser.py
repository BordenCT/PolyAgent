from decimal import Decimal

from polyagent.services.quant.strike.parser import (
    ParsedStrike, StrikeKind, cluster_key, parse_question,
)


def test_parses_btc_above():
    p = parse_question("Will the price of Bitcoin be above $50,000 on Friday?")
    assert p == ParsedStrike(
        asset_id="BTC", kind=StrikeKind.UP, strike=Decimal("50000"),
        resolution_date="Friday",
    )


def test_parses_eth_below():
    p = parse_question("Will the price of Ethereum be below $3,000 on Sunday?")
    assert p == ParsedStrike(
        asset_id="ETH", kind=StrikeKind.DOWN, strike=Decimal("3000"),
        resolution_date="Sunday",
    )


def test_parses_between_normalizes_low_high():
    p = parse_question("Will the price of BTC be between $80,000 and $70,000 on June 1?")
    assert p == ParsedStrike(
        asset_id="BTC", kind=StrikeKind.RANGE,
        strike=Decimal("70000"), upper_strike=Decimal("80000"),
        resolution_date="June 1",
    )


def test_unknown_asset_returns_none():
    assert parse_question("Will the price of Solana be above $200 on Friday?") is None


def test_unmatched_pattern_returns_none():
    assert parse_question("Random unrelated question?") is None


def test_empty_question_returns_none():
    assert parse_question("") is None


def test_resolution_date_optional_when_missing():
    """A strike question without 'on <date>' still parses; date is None."""
    p = parse_question("Will the price of Bitcoin be above $50,000?")
    assert p is not None
    assert p.asset_id == "BTC"
    assert p.resolution_date is None


def test_resolution_date_handles_multiword_date():
    p = parse_question("Will the price of Bitcoin be above $80,000 on May 6, 2026?")
    assert p is not None
    assert p.resolution_date == "May 6, 2026"


def test_cluster_key_for_strike_question():
    """Cluster key normalises the date to lowercase for collision-friendly
    hashing; the human-readable form lives on ParsedStrike.resolution_date."""
    key = cluster_key("Will the price of Bitcoin be above $80,000 on May 6?")
    assert key == ("BTC", "may 6")


def test_cluster_key_normalizes_case_for_date():
    """Two strikes on the same asset+date should collide on the same key
    regardless of question casing."""
    a = cluster_key("Will the price of Bitcoin be above $80,000 on May 6?")
    b = cluster_key("Will the price of bitcoin be below $82,000 on may 6?")
    assert a == b


def test_cluster_key_returns_none_for_non_strike():
    assert cluster_key("Will Eagles win Superbowl?") is None


def test_cluster_key_returns_none_when_date_missing():
    """Without a parseable date we cannot place the question in a cluster,
    so the executor should treat it as uncorrelated rather than guessing."""
    assert cluster_key("Will the price of Bitcoin be above $50,000?") is None


def test_cluster_key_distinguishes_assets_on_same_date():
    btc = cluster_key("Will the price of Bitcoin be above $80,000 on May 6?")
    eth = cluster_key("Will the price of Ethereum be above $2,400 on May 6?")
    assert btc != eth


def test_cluster_key_distinguishes_dates_on_same_asset():
    may6 = cluster_key("Will the price of Bitcoin be above $80,000 on May 6?")
    may7 = cluster_key("Will the price of Bitcoin be above $80,000 on May 7?")
    assert may6 != may7

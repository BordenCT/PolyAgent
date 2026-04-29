from decimal import Decimal

from polyagent.services.quant.strike.parser import (
    ParsedStrike, StrikeKind, parse_question,
)


def test_parses_btc_above():
    p = parse_question("Will the price of Bitcoin be above $50,000 on Friday?")
    assert p == ParsedStrike(asset_id="BTC", kind=StrikeKind.UP, strike=Decimal("50000"))


def test_parses_eth_below():
    p = parse_question("Will the price of Ethereum be below $3,000 on Sunday?")
    assert p == ParsedStrike(asset_id="ETH", kind=StrikeKind.DOWN, strike=Decimal("3000"))


def test_parses_between_normalizes_low_high():
    p = parse_question("Will the price of BTC be between $80,000 and $70,000 on June 1?")
    assert p == ParsedStrike(
        asset_id="BTC", kind=StrikeKind.RANGE,
        strike=Decimal("70000"), upper_strike=Decimal("80000"),
    )


def test_unknown_asset_returns_none():
    assert parse_question("Will the price of Solana be above $200 on Friday?") is None


def test_unmatched_pattern_returns_none():
    assert parse_question("Random unrelated question?") is None


def test_empty_question_returns_none():
    assert parse_question("") is None

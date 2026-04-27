"""Parser tests for crypto-strike questions."""
from decimal import Decimal

from polyagent.services.crypto_quant.parser import StrikeKind, parse_question


class TestParseQuestion:
    def test_above_btc(self):
        s = parse_question("Will the price of Bitcoin be above $80,000 on April 26?")
        assert s is not None
        assert s.asset == "BTC"
        assert s.kind == StrikeKind.UP
        assert s.strike == Decimal("80000")
        assert s.upper_strike is None

    def test_above_eth_with_decimal(self):
        s = parse_question("Will the price of Ethereum be above $2,400.50 on April 26?")
        assert s is not None
        assert s.asset == "ETH"
        assert s.kind == StrikeKind.UP
        assert s.strike == Decimal("2400.50")

    def test_below_btc(self):
        s = parse_question("Will the price of Bitcoin be below $70,000 on April 26?")
        assert s is not None
        assert s.kind == StrikeKind.DOWN
        assert s.strike == Decimal("70000")

    def test_between_btc(self):
        s = parse_question("Will the price of Bitcoin be between $74,000 and $76,000 on April 26?")
        assert s is not None
        assert s.kind == StrikeKind.RANGE
        assert s.strike == Decimal("74000")
        assert s.upper_strike == Decimal("76000")

    def test_between_swaps_low_and_high(self):
        # Polymarket sometimes phrases range markets with the larger first.
        s = parse_question("Will the price of Bitcoin be between $76,000 and $74,000 on April 26?")
        assert s is not None
        assert s.kind == StrikeKind.RANGE
        assert s.strike == Decimal("74000")
        assert s.upper_strike == Decimal("76000")

    def test_btc_alias(self):
        s = parse_question("Will the price of BTC be above $80,000 on April 26?")
        assert s is not None
        assert s.asset == "BTC"

    def test_case_insensitive(self):
        s = parse_question("WILL THE PRICE OF BITCOIN BE ABOVE $80,000 ON APRIL 26?")
        assert s is not None
        assert s.asset == "BTC"

    def test_non_matching_returns_none(self):
        assert parse_question("Will Norrie win the Madrid Open?") is None
        assert parse_question("Will Bitcoin reach $80,000 on April 26?") is None  # barrier-touch deferred
        assert parse_question("Will Bitcoin dip to $70,000 on April 24?") is None

    def test_empty_returns_none(self):
        assert parse_question("") is None
        assert parse_question(None) is None  # type: ignore[arg-type]

    def test_unknown_asset_returns_none(self):
        # Solana not in supported asset map.
        assert parse_question("Will the price of Solana be above $200 on April 26?") is None

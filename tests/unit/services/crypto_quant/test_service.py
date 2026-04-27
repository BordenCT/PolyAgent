"""End-to-end CryptoQuantService tests with a mocked spot source."""
from decimal import Decimal
from unittest.mock import MagicMock

from polyagent.services.crypto_quant import CryptoQuantService


class TestCryptoQuantService:
    def setup_method(self):
        self.btc_spot = MagicMock()
        self.eth_spot = MagicMock()
        self.svc = CryptoQuantService(
            btc_spot=self.btc_spot,
            eth_spot=self.eth_spot,
            btc_vol=0.60,
            eth_vol=0.75,
        )

    def test_evaluate_up_strike_does_not_crash(self):
        """Regression: dict-literal in _build_thesis_text evaluated all branches,
        crashing on upper_strike=None for UP markets via `f"{None:,}"`."""
        self.btc_spot.current.return_value = Decimal("74000")
        out = self.svc.evaluate(
            "Will the price of Bitcoin be above $80,000 on April 26?",
            hours_to_resolution=24.0,
        )
        assert out is not None
        strike, result, thesis_text = out
        assert strike.asset == "BTC"
        assert "spot >= $80,000" in thesis_text
        assert 0.0 <= result.probability <= 1.0

    def test_evaluate_down_strike_does_not_crash(self):
        self.btc_spot.current.return_value = Decimal("74000")
        out = self.svc.evaluate(
            "Will the price of Bitcoin be below $70,000 on April 26?",
            hours_to_resolution=24.0,
        )
        assert out is not None
        _, _, thesis_text = out
        assert "spot < $70,000" in thesis_text

    def test_evaluate_range_strike_includes_both_strikes(self):
        self.btc_spot.current.return_value = Decimal("75000")
        out = self.svc.evaluate(
            "Will the price of Bitcoin be between $74,000 and $76,000 on April 26?",
            hours_to_resolution=24.0,
        )
        assert out is not None
        _, _, thesis_text = out
        assert "$74,000" in thesis_text
        assert "$76,000" in thesis_text

    def test_evaluate_eth_uses_eth_spot_source(self):
        self.eth_spot.current.return_value = Decimal("2300")
        out = self.svc.evaluate(
            "Will the price of Ethereum be above $2,400 on April 26?",
            hours_to_resolution=24.0,
        )
        assert out is not None
        # Confirm we hit the ETH source, not BTC.
        self.btc_spot.current.assert_not_called()
        self.eth_spot.current.assert_called_once()

    def test_evaluate_returns_none_when_spot_missing(self):
        self.btc_spot.current.return_value = None
        out = self.svc.evaluate(
            "Will the price of Bitcoin be above $80,000 on April 26?",
            hours_to_resolution=24.0,
        )
        assert out is None

    def test_evaluate_returns_none_for_non_crypto_question(self):
        out = self.svc.evaluate(
            "Madrid Open: Sinner vs Bonzi",
            hours_to_resolution=24.0,
        )
        assert out is None

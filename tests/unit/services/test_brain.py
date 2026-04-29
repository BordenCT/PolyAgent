"""Tests for the brain service."""
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from polyagent.models import MarketData, ThesisChecks
from polyagent.services.brain import BrainService


class TestBrainService:
    def setup_method(self):
        self.claude = MagicMock()
        self.embeddings = MagicMock()
        self.historical_repo = MagicMock()
        self.brain = BrainService(
            llm_evaluator=self.claude,
            embeddings_service=self.embeddings,
            historical_repo=self.historical_repo,
            confidence_threshold=0.75,
            min_checks=3,
            min_edge=0.03,
        )

    def _make_market(self) -> MarketData:
        return MarketData(
            polymarket_id="0x1",
            question="Will BTC hit 150k?",
            category="crypto",
            token_id="t1",
            midpoint_price=Decimal("0.40"),
            bids_depth=Decimal("2000"),
            asks_depth=Decimal("1800"),
            hours_to_resolution=48.0,
            volume_24h=Decimal("100000"),
        )

    def test_evaluate_passes_with_3_checks(self):
        self.embeddings.embed_text.return_value = [0.1] * 1024
        self.historical_repo.find_similar.return_value = []
        self.claude.evaluate_market.return_value = {
            "base_rate": True,
            "news": True,
            "whale": False,
            "disposition": True,
            "probability": 0.78,
            "confidence": 0.82,
            "thesis": "Strong base rate",
        }

        result = self.brain.evaluate(self._make_market(), market_db_id=uuid4())
        assert result is not None
        assert result.checks.passed_count == 3
        assert result.claude_estimate == 0.78
        assert result.confidence == 0.82

    def test_evaluate_rejects_below_min_checks(self):
        self.embeddings.embed_text.return_value = [0.1] * 1024
        self.historical_repo.find_similar.return_value = []
        self.claude.evaluate_market.return_value = {
            "base_rate": True,
            "news": False,
            "whale": False,
            "disposition": False,
            "probability": 0.55,
            "confidence": 0.40,
            "thesis": "Weak signal",
        }

        result = self.brain.evaluate(self._make_market(), market_db_id=uuid4())
        assert result is None

    def test_evaluate_rejects_low_confidence(self):
        self.embeddings.embed_text.return_value = [0.1] * 1024
        self.historical_repo.find_similar.return_value = []
        self.claude.evaluate_market.return_value = {
            "base_rate": True,
            "news": True,
            "whale": True,
            "disposition": True,
            "probability": 0.60,
            "confidence": 0.50,  # below 0.75 threshold
            "thesis": "All checks pass but low confidence",
        }

        result = self.brain.evaluate(self._make_market(), market_db_id=uuid4())
        assert result is None

    def test_evaluate_rejects_tiny_edge(self):
        """Reject rubber-stamp evaluations where model probability ~= market price."""
        self.embeddings.embed_text.return_value = [0.1] * 1024
        self.historical_repo.find_similar.return_value = []
        self.claude.evaluate_market.return_value = {
            "base_rate": True,
            "news": True,
            "whale": True,
            "disposition": True,
            "probability": 0.41,  # market is 0.40 — edge = 0.01, below 0.03 gate
            "confidence": 0.90,
            "thesis": "Barely disagrees with the market",
        }

        result = self.brain.evaluate(self._make_market(), market_db_id=uuid4())
        assert result is None


class TestBrainCryptoQuantRouting:
    """When a CryptoQuantService is configured, crypto-strike markets must
    be routed to the deterministic estimator and the LLM must NOT be called."""

    def setup_method(self):
        self.claude = MagicMock()
        self.embeddings = MagicMock()
        self.historical_repo = MagicMock()
        self.crypto_quant = MagicMock()
        self.brain = BrainService(
            llm_evaluator=self.claude,
            embeddings_service=self.embeddings,
            historical_repo=self.historical_repo,
            confidence_threshold=0.75,
            min_checks=3,
            min_edge=0.03,
            crypto_quant=self.crypto_quant,
        )

    def _make_market(self, question: str, price: str = "0.20") -> MarketData:
        return MarketData(
            polymarket_id="0xCRYPTO",
            question=question,
            category="crypto",
            token_id="t1",
            midpoint_price=Decimal(price),
            bids_depth=Decimal("2000"),
            asks_depth=Decimal("1800"),
            hours_to_resolution=24.0,
            volume_24h=Decimal("50000"),
        )

    def test_crypto_strike_routes_to_quant_and_skips_llm(self):
        from polyagent.services.quant.strike import (
            ParsedStrike, QuantResult, StrikeKind,
        )

        question = "Will the price of Bitcoin be above $80,000 on April 26?"
        strike = ParsedStrike(asset_id="BTC", kind=StrikeKind.UP, strike=Decimal("80000"))
        self.crypto_quant.matches.return_value = strike
        self.crypto_quant.evaluate.return_value = (
            strike,
            QuantResult(probability=0.05, confidence=0.95, sigma_distance=2.5),
            "[crypto_quant] BTC spot >= $80,000 ...",
        )

        market = self._make_market(question, price="0.30")  # edge = 0.25
        result = self.brain.evaluate(market, market_db_id=uuid4())

        assert result is not None
        assert result.claude_estimate == 0.05
        assert result.confidence == 0.95
        assert result.checks.passed_count == 4
        # LLM must not be invoked for crypto-strike questions.
        self.claude.evaluate_market.assert_not_called()

    def test_crypto_strike_with_thin_edge_is_rejected_without_llm_fallback(self):
        from polyagent.services.quant.strike import (
            ParsedStrike, QuantResult, StrikeKind,
        )

        question = "Will the price of Bitcoin be above $80,000 on April 26?"
        strike = ParsedStrike(asset_id="BTC", kind=StrikeKind.UP, strike=Decimal("80000"))
        self.crypto_quant.matches.return_value = strike
        self.crypto_quant.evaluate.return_value = (
            strike,
            QuantResult(probability=0.21, confidence=0.95, sigma_distance=0.5),
            "...",
        )
        # market price 0.20, quant 0.21 → edge 0.01 below 0.03 gate
        market = self._make_market(question, price="0.20")
        result = self.brain.evaluate(market, market_db_id=uuid4())

        assert result is None
        # LLM is NOT a fallback for crypto-strike rejections.
        self.claude.evaluate_market.assert_not_called()

    def test_non_crypto_market_falls_through_to_llm(self):
        self.crypto_quant.matches.return_value = None  # not a crypto-strike question
        self.embeddings.embed_text.return_value = [0.1] * 1024
        self.historical_repo.find_similar.return_value = []
        self.claude.evaluate_market.return_value = {
            "base_rate": True, "news": True, "whale": True, "disposition": True,
            "probability": 0.55, "confidence": 0.90, "thesis": "...",
        }

        market = self._make_market("Madrid Open: Sinner vs Bonzi", price="0.30")
        result = self.brain.evaluate(market, market_db_id=uuid4())

        assert result is not None
        self.claude.evaluate_market.assert_called_once()

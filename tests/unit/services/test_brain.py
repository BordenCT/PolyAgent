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

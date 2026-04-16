"""Tests for Claude API client."""
from unittest.mock import MagicMock, patch

from polyagent.data.clients.claude import ClaudeClient


class TestClaudeClient:
    def setup_method(self):
        self.mock_anthropic = MagicMock()
        self.client = ClaudeClient.__new__(ClaudeClient)
        self.client._client = self.mock_anthropic
        self.client._model = "claude-sonnet-4-20250514"

    def test_estimate_probability_returns_float(self):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"probability": 0.72}')]
        self.mock_anthropic.messages.create.return_value = mock_response

        result = self.client.estimate_probability(
            question="Will BTC hit 150k?",
            context="Current price: $98k, trending up",
        )
        assert result == 0.72

    def test_estimate_probability_handles_bad_json(self):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="I think about 70%")]
        self.mock_anthropic.messages.create.return_value = mock_response

        result = self.client.estimate_probability(
            question="test?",
            context="test",
        )
        # Should extract number from text as fallback
        assert 0.0 <= result <= 1.0

    def test_evaluate_market_returns_checks(self):
        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(
                text='{"base_rate": true, "news": true, "whale": false, '
                '"disposition": true, "probability": 0.78, "confidence": 0.82, '
                '"thesis": "Strong base rate with news catalyst"}'
            )
        ]
        self.mock_anthropic.messages.create.return_value = mock_response

        result = self.client.evaluate_market(
            question="Will X happen?",
            market_price=0.45,
            rag_context="Similar markets resolved YES 70% of the time",
            whale_activity="2 target wallets active",
        )
        assert result["base_rate"] is True
        assert result["news"] is True
        assert result["whale"] is False
        assert result["disposition"] is True
        assert result["probability"] == 0.78
        assert result["confidence"] == 0.82

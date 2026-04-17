"""Tests for Ollama client."""
from unittest.mock import MagicMock, patch

from polyagent.data.clients.ollama import OllamaClient


class TestOllamaClient:
    def setup_method(self):
        self.client = OllamaClient.__new__(OllamaClient)
        self.client._model = "phi4:14b"
        self.client._base_url = "http://localhost:11434"
        self.client._http = MagicMock()

    def test_parse_clean_json(self):
        assert self.client._parse_probability('{"probability": 0.72}') == 0.72

    def test_parse_json_in_code_block(self):
        text = '```json\n{"probability": 0.65}\n```'
        assert self.client._parse_probability(text) == 0.65

    def test_parse_raw_decimal(self):
        assert self.client._parse_probability("I think about 0.78") == 0.78

    def test_parse_clamps_above_one(self):
        assert self.client._parse_probability('{"probability": 1.5}') == 1.0

    def test_parse_clamps_below_zero(self):
        assert self.client._parse_probability('{"probability": -0.3}') == 0.0

    def test_parse_garbage_returns_default(self):
        assert self.client._parse_probability("no numbers here") == 0.5

    def test_estimate_probability_calls_api(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": '{"probability": 0.68}'}
        mock_resp.raise_for_status = MagicMock()
        self.client._http.post.return_value = mock_resp

        result = self.client.estimate_probability("Will BTC hit 150k?")
        assert result == 0.68
        self.client._http.post.assert_called_once()

    def test_estimate_probability_fallback_on_error(self):
        import httpx
        self.client._http.post.side_effect = httpx.ConnectError("Connection refused")

        result = self.client.estimate_probability("test?")
        assert result == 0.5  # fallback

    def test_estimate_batch(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": '{"probability": 0.70}'}
        mock_resp.raise_for_status = MagicMock()
        self.client._http.post.return_value = mock_resp

        questions = [
            {"id": "0x1", "question": "Q1?"},
            {"id": "0x2", "question": "Q2?"},
        ]
        results = self.client.estimate_batch(questions)
        assert len(results) == 2
        assert results["0x1"] == 0.70
        assert results["0x2"] == 0.70

    def test_health_check_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        self.client._http.get.return_value = mock_resp

        assert self.client.health_check() is True

    def test_health_check_failure(self):
        import httpx
        self.client._http.get.side_effect = httpx.ConnectError("Connection refused")

        assert self.client.health_check() is False

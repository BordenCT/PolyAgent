"""Tests for Ollama client."""
import json
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


class TestEvaluateMarketTwoCall:
    """The two-call flow: strict initial pass + self-critique."""

    def setup_method(self):
        self.client = OllamaClient.__new__(OllamaClient)
        self.client._model = "phi4:14b"
        self.client._base_url = "http://localhost:11434"
        self.client._http = MagicMock()

    def _make_resp(self, body_text: str) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"response": body_text}
        resp.raise_for_status = MagicMock()
        return resp

    def _set_responses(self, *bodies: str) -> None:
        """Queue responses for sequential .post() calls."""
        self.client._http.post.side_effect = [self._make_resp(b) for b in bodies]

    def test_two_calls_made_in_order(self):
        initial = json.dumps({
            "base_rate": True, "news": False, "whale": False, "disposition": False,
            "probability": 0.42, "confidence": 0.65,
            "evidence": "Specific historical record cited", "counterargument": "n/a",
            "thesis": "Initial thesis",
        })
        critique = json.dumps({
            "probability": 0.42, "confidence": 0.65, "critique": "no change",
        })
        self._set_responses(initial, critique)

        result = self.client.evaluate_market(
            question="Some market?", market_price=0.30,
            rag_context="No similar historical markets found.",
            whale_activity="No whale activity data available for this market.",
        )

        assert self.client._http.post.call_count == 2
        assert result["probability"] == 0.42
        assert result["confidence"] == 0.65
        assert result["base_rate"] is True
        assert result["thesis"] == "Initial thesis"  # critique said "no change"

    def test_critique_lowers_confidence(self):
        initial = json.dumps({
            "base_rate": False, "news": False, "whale": False, "disposition": True,
            "probability": 0.55, "confidence": 0.80,
            "evidence": "Generic 'market overconfident' reasoning",
            "counterargument": "weak strawman",
            "thesis": "Buy the dip",
        })
        critique = json.dumps({
            "probability": 0.30, "confidence": 0.20,
            "critique": "Evidence is generic; original was an evidence-free hunch.",
        })
        self._set_responses(initial, critique)

        result = self.client.evaluate_market(
            question="Q", market_price=0.30, rag_context="", whale_activity="",
        )

        # Critique values are authoritative.
        assert result["probability"] == 0.30
        assert result["confidence"] == 0.20
        assert "critique" in result["thesis"]

    def test_critique_failure_falls_back_to_initial(self):
        import httpx
        initial = json.dumps({
            "base_rate": True, "news": False, "whale": False, "disposition": False,
            "probability": 0.42, "confidence": 0.65,
            "evidence": "Cited evidence", "counterargument": "n/a",
            "thesis": "Initial thesis",
        })
        # Initial succeeds, critique fails.
        self.client._http.post.side_effect = [
            self._make_resp(initial),
            httpx.ConnectError("Connection refused"),
        ]

        result = self.client.evaluate_market(
            question="Q", market_price=0.30, rag_context="", whale_activity="",
        )

        assert result["probability"] == 0.42
        assert result["confidence"] == 0.65
        assert result["thesis"] == "Initial thesis"

    def test_initial_failure_returns_no_trade_default(self):
        import httpx
        self.client._http.post.side_effect = httpx.ConnectError("Connection refused")

        result = self.client.evaluate_market(
            question="Q", market_price=0.30, rag_context="", whale_activity="",
        )

        # No-trade default: probability = market_price, confidence = 0
        assert result["probability"] == 0.30
        assert result["confidence"] == 0.0
        assert result["base_rate"] is False
        assert "HOLD" in result["thesis"]

    def test_unparseable_initial_response_returns_no_trade(self):
        # Initial returns garbage; critique never runs (or doesn't matter).
        self._set_responses("not valid json at all", "{}")

        result = self.client.evaluate_market(
            question="Q", market_price=0.30, rag_context="", whale_activity="",
        )

        # Defaults: probability = market_price, confidence = 0
        assert result["probability"] == 0.30
        assert result["confidence"] == 0.0

    def test_eval_prompt_includes_carve_out_language(self):
        """Sanity: the carve-out for favorite-longshot bias must be in the prompt."""
        initial = json.dumps({
            "base_rate": False, "news": False, "whale": False, "disposition": True,
            "probability": 0.92, "confidence": 0.65,
            "evidence": "Favorite-longshot structural bias",
            "counterargument": "n/a", "thesis": "Sell favorite",
        })
        critique = json.dumps({"probability": 0.92, "confidence": 0.65, "critique": "no change"})
        self._set_responses(initial, critique)

        self.client.evaluate_market(
            question="Will Sinner win?", market_price=0.95,
            rag_context="", whale_activity="",
        )

        first_call_prompt = self.client._http.post.call_args_list[0][1]["json"]["prompt"]
        assert "favorite-longshot" in first_call_prompt.lower()
        assert "0.90" in first_call_prompt
        assert "HOLD" in first_call_prompt

    def test_critique_prompt_includes_initial_outputs(self):
        initial = json.dumps({
            "base_rate": True, "news": False, "whale": False, "disposition": False,
            "probability": 0.42, "confidence": 0.65,
            "evidence": "Specific evidence X", "counterargument": "Counter Y",
            "thesis": "Buy because Z",
        })
        critique = json.dumps({"probability": 0.42, "confidence": 0.65, "critique": "no change"})
        self._set_responses(initial, critique)

        self.client.evaluate_market(
            question="Q", market_price=0.30, rag_context="", whale_activity="",
        )

        critique_prompt = self.client._http.post.call_args_list[1][1]["json"]["prompt"]
        assert "Specific evidence X" in critique_prompt
        assert "Counter Y" in critique_prompt
        assert "Buy because Z" in critique_prompt

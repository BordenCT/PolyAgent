"""Ollama client for local LLM inference (phi4:14b).

Used for scanner probability estimates — zero cost, low latency.
Claude Sonnet is reserved for the brain's deep 4-check evaluation.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

logger = logging.getLogger("polyagent.clients.ollama")


class OllamaClient:
    """Wraps the Ollama REST API for local model inference."""

    def __init__(self, base_url: str = "http://192.168.1.56:11434", model: str = "phi4:14b") -> None:
        self._base_url = base_url
        self._model = model
        self._http = httpx.Client(base_url=base_url, timeout=60.0)

    def estimate_probability(self, question: str, context: str = "") -> float:
        """Quick probability estimate for a market question.

        Returns a float between 0.0 and 1.0.
        """
        prompt = (
            "You are a prediction market probability estimator. "
            "Given a market question, estimate the probability of YES occurring. "
            "Always return a number — use 0.5 if genuinely uncertain. "
            "Return ONLY a JSON object: {\"probability\": 0.XX}\n\n"
            f"Question: {question}\n"
        )
        if context:
            prompt += f"Context: {context}\n"
        prompt += "\nReturn ONLY valid JSON. probability must be a number 0.0-1.0, never null."

        try:
            resp = self._http.post(
                "/api/generate",
                json={
                    "model": self._model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                    "options": {
                        "temperature": 0.1,
                        "num_predict": 64,
                    },
                },
            )
            resp.raise_for_status()
            text = resp.json().get("response", "")
            return self._parse_probability(text)
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            logger.warning("Ollama estimate failed: %s", e)
            return 0.5  # fallback to midpoint

    def evaluate_market(
        self,
        question: str,
        market_price: float,
        rag_context: str,
        whale_activity: str,
    ) -> dict:
        """Full 4-check market evaluation via Ollama. Matches ClaudeClient interface."""
        has_rag = bool(rag_context) and "No similar historical markets" not in rag_context
        has_whale = bool(whale_activity) and "No whale activity" not in whale_activity

        prompt = (
            "You are an expert prediction market analyst. Your job is to estimate the "
            "probability of YES for the market below and decide whether there is an edge "
            "versus the current market price.\n\n"
            "Return ONLY valid JSON with four boolean 'red-flag' checks, a probability, "
            "a confidence, and a short thesis.\n\n"
            "Check semantics (true = NO red flag, safe to proceed; false = red flag found):\n"
            "  - base_rate: true unless general knowledge of similar events clearly "
            "contradicts the direction implied by the price.\n"
            "  - news: true unless you are aware of recent news that clearly invalidates "
            "the direction implied by the price. Absence of news evidence = true.\n"
            "  - whale: true unless provided whale data contradicts the position. "
            "Absence of whale data = true.\n"
            "  - disposition: true if you see a plausible cognitive error or mispricing "
            "(anchoring, recency, availability, extreme pricing without justification). "
            "Otherwise false.\n\n"
            "Confidence rules:\n"
            "  - Confidence reflects how sure you are of YOUR probability estimate given the "
            "question and market price. It must NOT be penalized for missing news/whale/"
            "historical data.\n"
            "  - If the question is clear and the price looks obviously mispriced relative "
            "to common-sense priors, confidence should be >= 0.75.\n"
            "  - Only go below 0.60 if the question itself is genuinely ambiguous or the "
            "price seems defensible.\n\n"
            f"Question: {question}\n"
            f"Current market price (YES): {market_price:.4f}\n"
        )
        if has_rag:
            prompt += f"\nHistorical Context:\n{rag_context}\n"
        if has_whale:
            prompt += f"\nWhale Activity:\n{whale_activity}\n"
        prompt += (
            '\nReturn ONLY JSON: {"base_rate": true/false, "news": true/false, '
            '"whale": true/false, "disposition": true/false, '
            '"probability": 0.XX, "confidence": 0.XX, '
            '"thesis": "your 1-2 sentence thesis"}'
        )

        try:
            resp = self._http.post(
                "/api/generate",
                json={
                    "model": self._model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0.1, "num_predict": 256},
                },
            )
            resp.raise_for_status()
            text = resp.json().get("response", "")
            logger.debug("Ollama raw evaluation: %s", text)
            return self._parse_evaluation(text)
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            logger.warning("Ollama evaluate_market failed: %s", e)
            return {
                "base_rate": False, "news": False, "whale": False,
                "disposition": False, "probability": 0.5,
                "confidence": 0.0, "thesis": "Evaluation failed",
            }

    def _parse_evaluation(self, text: str) -> dict:
        """Parse a full 4-check evaluation response."""
        defaults = {
            "base_rate": False, "news": False, "whale": False,
            "disposition": False, "probability": 0.5,
            "confidence": 0.0, "thesis": "",
        }
        try:
            data = json.loads(text)
            for key in defaults:
                if key in data:
                    defaults[key] = data[key]
            defaults["probability"] = max(0.0, min(1.0, float(defaults["probability"])))
            defaults["confidence"] = max(0.0, min(1.0, float(defaults["confidence"])))
            return defaults
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        # Try extracting JSON from markdown code block
        json_match = re.search(r"\{[^}]*\}", text, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group())
                for key in defaults:
                    if key in data:
                        defaults[key] = data[key]
                defaults["probability"] = max(0.0, min(1.0, float(defaults["probability"])))
                defaults["confidence"] = max(0.0, min(1.0, float(defaults["confidence"])))
                return defaults
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        logger.warning("Could not parse evaluation from: %s", text[:200])
        return defaults

    def estimate_batch(self, questions: list[dict[str, str]]) -> dict[str, float]:
        """Estimate probabilities for multiple markets.

        Args:
            questions: List of dicts with 'id' and 'question' keys.

        Returns:
            Dict mapping market_id -> probability.
        """
        estimates = {}
        for q in questions:
            market_id = q["id"]
            question = q["question"]
            estimates[market_id] = self.estimate_probability(question)
        return estimates

    def health_check(self) -> bool:
        """Check if Ollama is reachable."""
        try:
            resp = self._http.get("/api/tags")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def _parse_probability(self, text: str) -> float:
        """Extract probability from model response."""
        # Try JSON parse first
        try:
            data = json.loads(text)
            p = data.get("probability")
            if p is not None:
                return max(0.0, min(1.0, float(p)))
            return 0.5  # model returned null — treat as uncertain
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        # Try extracting JSON from markdown code block
        json_match = re.search(r"\{[^}]*\"probability\"\s*:\s*([\d.]+)[^}]*\}", text)
        if json_match:
            try:
                p = float(json_match.group(1))
                return max(0.0, min(1.0, p))
            except ValueError:
                pass

        # Fallback: extract any decimal 0.XX
        match = re.search(r"0\.\d+", text)
        if match:
            return max(0.0, min(1.0, float(match.group())))

        logger.warning("Could not parse probability from: %s", text[:100])
        return 0.5

    def close(self) -> None:
        """Close the HTTP client."""
        self._http.close()

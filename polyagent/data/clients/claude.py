"""Claude API client with prompt caching."""
from __future__ import annotations

import json
import logging
import re

import anthropic

logger = logging.getLogger("polyagent.clients.claude")

SYSTEM_PROMPT = """You are an expert prediction market analyst. Your job is to evaluate
Polymarket markets and estimate probabilities.

You will be given a market question, current price, historical context from similar markets,
and whale wallet activity. You must run 4 checks and return a structured JSON response.

The 4 checks:
1. base_rate — Does historical data support this outcome? Look at similar resolved markets.
2. news — Has anything changed in the last 6 hours that affects this market?
3. whale — Are high-performing wallets active in this market? What positions are they taking?
4. disposition — Is the crowd making a cognitive error (anchoring, recency bias, availability bias)?

Return ONLY valid JSON with this exact structure:
{
    "base_rate": true/false,
    "news": true/false,
    "whale": true/false,
    "disposition": true/false,
    "probability": 0.XX,
    "confidence": 0.XX,
    "thesis": "Your 1-2 sentence thesis explaining the opportunity"
}

probability = your estimated probability of YES outcome (0.0 to 1.0)
confidence = how confident you are in your estimate (0.0 to 1.0)
"""


class ClaudeClient:
    """Wraps the Anthropic SDK with prompt caching for market evaluation."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514") -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def estimate_probability(self, question: str, context: str) -> float:
        """Quick probability estimate for a market question.

        Args:
            question: The market question to evaluate.
            context: Relevant context for the estimate.

        Returns:
            Estimated probability of YES outcome (0.0–1.0).
        """
        response = self._client.messages.create(
            model=self._model,
            max_tokens=256,
            system=[
                {
                    "type": "text",
                    "text": "You estimate probabilities for prediction markets. "
                    'Return ONLY JSON: {"probability": 0.XX}',
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": f"Question: {question}\nContext: {context}\n"
                    "Estimate the probability of YES (0.0-1.0).",
                }
            ],
        )

        text = response.content[0].text
        try:
            data = json.loads(text)
            return float(data["probability"])
        except (json.JSONDecodeError, KeyError):
            # Fallback: extract first decimal from text
            match = re.search(r"0\.\d+", text)
            if match:
                return float(match.group())
            return 0.5

    def evaluate_market(
        self,
        question: str,
        market_price: float,
        rag_context: str,
        whale_activity: str,
    ) -> dict:
        """Full 4-check market evaluation with prompt caching.

        Args:
            question: The market question to evaluate.
            market_price: Current mid price of the market.
            rag_context: Historical outcomes from similar markets.
            whale_activity: Summary of whale wallet activity.

        Returns:
            Dict with keys: base_rate, news, whale, disposition, probability,
            confidence, thesis.

        Raises:
            ValueError: If Claude's response cannot be parsed as JSON.
        """
        user_prompt = (
            f"## Market\nQuestion: {question}\n"
            f"Current market price: {market_price:.4f}\n\n"
            f"## Historical Context (similar resolved markets)\n{rag_context}\n\n"
            f"## Whale Activity\n{whale_activity}\n\n"
            "Run all 4 checks and return the JSON evaluation."
        )

        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_prompt}],
        )

        text = response.content[0].text
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Failed to parse Claude response as JSON: %s", text[:200])
            # Attempt to extract JSON from markdown code block
            json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(1))
            raise ValueError(f"Could not parse Claude response: {text[:200]}")

    def close(self) -> None:
        """Close the underlying Anthropic client."""
        self._client.close()

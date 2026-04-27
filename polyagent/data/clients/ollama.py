"""Ollama client for local LLM inference (phi4:14b).

The brain runs in two phases against Ollama:

1. Initial evaluation — strict, evidence-required prompt with HOLD as the
   default. Confidence is floored low when the model has no specific
   knowledge. A carve-out for the favorite-longshot bias at price >= 0.90
   on single-event sports preserves the structural SELL edge.
2. Self-critique — the model reads its own output and decides whether to
   lower confidence or move probability toward the market price. The
   critique's adjusted values are authoritative.

Two calls are slower than one. That is the point: the bot trades less and
trades better. If the SELL edge survives the strict evidence rule, it's
real. If it doesn't, it wasn't.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

logger = logging.getLogger("polyagent.clients.ollama")


_EVAL_PROMPT_TEMPLATE = """\
You are a prediction-market analyst. Your default action is HOLD. You do
NOT trade unless you can cite SPECIFIC EVIDENCE that the market is
mispriced. Plausible-sounding reasoning is not evidence.

Decision procedure (follow strictly):
1. Identify what specific information would justify disagreeing with the
   market price. Examples: a known recent result, a public statistic, a
   structural feature of the question.
2. State whether you actually possess that information. If not, your
   probability MUST equal the market price and your confidence MUST be
   below 0.50.
3. If you do possess it, write the counter-argument: what is the
   strongest case AGAINST your view? If the counter-argument is
   plausible, lower your confidence further.

Honesty rules:
- "Recency bias" / "anchoring" / "the market is overconfident" are NOT
  evidence. They are post-hoc rationalizations. Do not invoke them.
- Absence of evidence is NOT evidence of mispricing. Default to HOLD.
- Do not anchor on a default confidence. If you have no specific
  knowledge, confidence MUST be below 0.50.

Structural carve-out (favorite-longshot bias):
- If the YES price is >= 0.90 AND the question is a single-event sports
  outcome (single match, single fight, single race), the favorite-
  longshot bias is a STRUCTURAL mispricing of binary markets, not a
  cognitive one. You MAY treat that as evidence: mark `disposition`
  true, set probability slightly below market_price (e.g., market_price
  minus 0.03 to 0.05), and set confidence in [0.55, 0.75].
- This carve-out applies ONLY at YES price >= 0.90 AND only to single-
  event sports. It does NOT apply to crypto strikes, weather, geopolitics,
  tweet-count brackets, or any compound/multi-leg market.

Schema (true means: this check produced specific evidence supporting a
non-HOLD action; false means: no specific evidence found):
  base_rate    - cite a specific historical rate or statistic
  news         - cite a specific recent event
  whale        - cite specific whale activity from data provided
  disposition  - cite a specific structural mispricing (NOT a generic
                 cognitive-bias label)

Return ONLY this JSON:
{{
  "base_rate": true/false,
  "news": true/false,
  "whale": true/false,
  "disposition": true/false,
  "probability": 0.XX,
  "confidence": 0.XX,
  "evidence": "Specific evidence cited, or 'none'.",
  "counterargument": "Strongest case against this view, or 'n/a'.",
  "thesis": "1-2 sentences. If no evidence, say 'No specific evidence - HOLD.'"
}}

Question: {question}
Current market price (YES): {market_price:.4f}
{historical_block}{whale_block}"""


_CRITIQUE_PROMPT_TEMPLATE = """\
You wrote this evaluation for a prediction market. Critique it.

Question: {question}
Market price (YES): {market_price:.4f}

Your previous output:
  probability: {probability:.4f}
  confidence: {confidence:.4f}
  evidence: {evidence}
  counterargument: {counterargument}
  thesis: {thesis}

Critique these honestly:
1. Is the cited evidence concrete and verifiable, or is it a generic
   statement that could apply to any matchup?
2. Did the counter-argument engage with the strongest case AGAINST,
   or was it a strawman?
3. If the original probability was an evidence-free hunch, the adjusted
   probability MUST equal the market price and adjusted confidence MUST
   be below 0.50.
4. If the original cited a structural mispricing (e.g., favorite-longshot
   at price >= 0.90 on single-event sports), keep it; otherwise apply
   rule 3.

Return ONLY this JSON:
{{
  "probability": 0.XX,
  "confidence": 0.XX,
  "critique": "1-2 sentences. Use 'no change' if the original holds."
}}"""


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
            "Always return a number - use 0.5 if genuinely uncertain. "
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
        """Two-call evaluation: strict initial pass + self-critique adjustment.

        Returns the same dict shape the brain expects (base_rate, news, whale,
        disposition, probability, confidence, thesis). The critique's adjusted
        probability and confidence are authoritative; thesis is augmented with
        the critique note.

        On failure of either call, falls back to a no-trade dict
        (probability=market_price, confidence=0.0, all checks false).
        """
        has_rag = bool(rag_context) and "No similar historical markets" not in rag_context
        has_whale = bool(whale_activity) and "No whale activity" not in whale_activity

        historical_block = f"\nHistorical Context:\n{rag_context}\n" if has_rag else ""
        whale_block = f"\nWhale Activity:\n{whale_activity}\n" if has_whale else ""

        eval_prompt = _EVAL_PROMPT_TEMPLATE.format(
            question=question,
            market_price=market_price,
            historical_block=historical_block,
            whale_block=whale_block,
        )

        initial = self._post_json(eval_prompt, num_predict=512)
        if initial is None:
            return self._no_trade_default(market_price)

        parsed = self._parse_evaluation(initial, market_price)

        critique_prompt = _CRITIQUE_PROMPT_TEMPLATE.format(
            question=question,
            market_price=market_price,
            probability=parsed["probability"],
            confidence=parsed["confidence"],
            evidence=parsed.get("evidence", "none"),
            counterargument=parsed.get("counterargument", "n/a"),
            thesis=parsed.get("thesis", ""),
        )
        critique = self._post_json(critique_prompt, num_predict=192)
        if critique is not None:
            adj = self._parse_critique(critique, market_price)
            parsed["probability"] = adj["probability"]
            parsed["confidence"] = adj["confidence"]
            parsed["thesis"] = self._merge_thesis(parsed.get("thesis", ""), adj.get("critique", ""))
        else:
            logger.warning("Ollama critique call failed; using initial evaluation")

        # Drop fields the downstream brain doesn't expect.
        return {
            "base_rate": parsed.get("base_rate", False),
            "news": parsed.get("news", False),
            "whale": parsed.get("whale", False),
            "disposition": parsed.get("disposition", False),
            "probability": parsed["probability"],
            "confidence": parsed["confidence"],
            "thesis": parsed.get("thesis", ""),
        }

    def _post_json(self, prompt: str, num_predict: int) -> str | None:
        try:
            resp = self._http.post(
                "/api/generate",
                json={
                    "model": self._model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0.1, "num_predict": num_predict},
                },
            )
            resp.raise_for_status()
            return resp.json().get("response", "")
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            logger.warning("Ollama call failed: %s", e)
            return None

    @staticmethod
    def _no_trade_default(market_price: float) -> dict:
        return {
            "base_rate": False, "news": False, "whale": False,
            "disposition": False,
            "probability": float(market_price),
            "confidence": 0.0,
            "thesis": "Evaluation failed - HOLD.",
        }

    def _parse_evaluation(self, text: str, market_price: float) -> dict:
        """Parse the strict evaluation. Defaults to no-trade values on missing keys."""
        defaults = {
            "base_rate": False, "news": False, "whale": False,
            "disposition": False,
            "probability": float(market_price),
            "confidence": 0.0,
            "evidence": "none",
            "counterargument": "n/a",
            "thesis": "",
        }
        data = self._extract_json(text)
        if not data:
            logger.warning("Could not parse evaluation from: %s", text[:200])
            return defaults
        for key in defaults:
            if key in data:
                defaults[key] = data[key]
        defaults["probability"] = max(0.0, min(1.0, float(defaults["probability"])))
        defaults["confidence"] = max(0.0, min(1.0, float(defaults["confidence"])))
        return defaults

    def _parse_critique(self, text: str, market_price: float) -> dict:
        """Parse the critique JSON. On parse failure, force HOLD (probability = market price)."""
        defaults = {
            "probability": float(market_price),
            "confidence": 0.0,
            "critique": "parse failed - HOLD",
        }
        data = self._extract_json(text)
        if not data:
            logger.warning("Could not parse critique from: %s", text[:200])
            return defaults
        for key in defaults:
            if key in data:
                defaults[key] = data[key]
        defaults["probability"] = max(0.0, min(1.0, float(defaults["probability"])))
        defaults["confidence"] = max(0.0, min(1.0, float(defaults["confidence"])))
        return defaults

    @staticmethod
    def _merge_thesis(original: str, critique: str) -> str:
        if not critique or critique.strip().lower() == "no change":
            return original
        return f"{original} | critique: {critique}"

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        """Best-effort JSON extraction. Returns None if no usable object found."""
        if not text:
            return None
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            pass
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except (json.JSONDecodeError, TypeError):
                return None
        return None

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
        try:
            data = json.loads(text)
            p = data.get("probability")
            if p is not None:
                return max(0.0, min(1.0, float(p)))
            return 0.5  # model returned null - treat as uncertain
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        json_match = re.search(r"\{[^}]*\"probability\"\s*:\s*([\d.]+)[^}]*\}", text)
        if json_match:
            try:
                p = float(json_match.group(1))
                return max(0.0, min(1.0, p))
            except ValueError:
                pass

        match = re.search(r"0\.\d+", text)
        if match:
            return max(0.0, min(1.0, float(match.group())))

        logger.warning("Could not parse probability from: %s", text[:100])
        return 0.5

    def close(self) -> None:
        """Close the HTTP client."""
        self._http.close()

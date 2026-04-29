"""Brain service — 4-check market evaluation via LLM (Claude or Ollama).

For crypto-strike markets matching a known pattern, the brain consults a
deterministic Φ(d₂) estimator (CryptoQuantService) instead of the LLM. The
LLM has no live spot reference and reliably mis-prices these.
"""
from __future__ import annotations

import logging
from typing import Protocol
from uuid import UUID

from polyagent.models import Consensus, MarketData, Thesis, ThesisChecks
from polyagent.services.quant.strike import QuantStrikeService as CryptoQuantService
from polyagent.services.embeddings import EmbeddingsService

logger = logging.getLogger("polyagent.services.brain")


class LLMEvaluator(Protocol):
    """Interface for any LLM that can evaluate markets."""

    def evaluate_market(
        self,
        question: str,
        market_price: float,
        rag_context: str,
        whale_activity: str,
    ) -> dict: ...


class BrainService:
    """Evaluates markets using LLM 4-check analysis.

    Supports both Claude and Ollama as the LLM backend via the LLMEvaluator protocol.
    """

    def __init__(
        self,
        llm_evaluator: LLMEvaluator,
        embeddings_service: EmbeddingsService,
        historical_repo,
        confidence_threshold: float = 0.75,
        min_checks: int = 3,
        min_edge: float = 0.03,
        crypto_quant: CryptoQuantService | None = None,
    ) -> None:
        self._llm = llm_evaluator
        self._embeddings = embeddings_service
        self._historical_repo = historical_repo
        self._confidence_threshold = confidence_threshold
        self._min_checks = min_checks
        self._min_edge = min_edge
        self._crypto_quant = crypto_quant

    def evaluate(self, market: MarketData, market_db_id: UUID) -> Thesis | None:
        """Run 4-check evaluation on a market. Returns Thesis or None if rejected.

        Routes crypto-strike markets to the deterministic quant pipeline when
        configured; the LLM is never consulted for those — its verdict on
        strike-ladder markets is unreliable.
        """
        if self._crypto_quant is not None and self._crypto_quant.matches(market.question):
            return self._evaluate_via_crypto_quant(market, market_db_id)
        # Build RAG context from similar historical outcomes (skipped if embeddings disabled)
        embedding = self._embeddings.embed_text(market.question)
        similar = self._historical_repo.find_similar(embedding, limit=10) if embedding else []
        rag_context = self._format_rag_context(similar)

        # Get whale activity context
        whale_context = self._get_whale_context(market)

        # Call LLM for full evaluation
        result = self._llm.evaluate_market(
            question=market.question,
            market_price=float(market.midpoint_price),
            rag_context=rag_context,
            whale_activity=whale_context,
        )

        checks = ThesisChecks(
            base_rate=result.get("base_rate", False),
            news=result.get("news", False),
            whale=result.get("whale", False),
            disposition=result.get("disposition", False),
        )

        probability = result.get("probability", 0.5)
        confidence = result.get("confidence", 0.0)
        thesis_text = result.get("thesis", "")

        # Apply gates
        if checks.passed_count < self._min_checks:
            logger.info(
                "REJECT %s — only %d/%d checks passed",
                market.polymarket_id,
                checks.passed_count,
                self._min_checks,
            )
            return None

        if confidence < self._confidence_threshold:
            logger.info(
                "REJECT %s — confidence %.2f below %.2f threshold",
                market.polymarket_id,
                confidence,
                self._confidence_threshold,
            )
            return None

        edge = abs(probability - float(market.midpoint_price))
        if edge < self._min_edge:
            logger.info(
                "REJECT %s — edge %.4f below %.4f threshold (p=%.4f price=%.4f)",
                market.polymarket_id,
                edge,
                self._min_edge,
                probability,
                float(market.midpoint_price),
            )
            return None

        thesis = Thesis.create(
            market_id=market_db_id,
            claude_estimate=probability,
            confidence=confidence,
            checks=checks,
            thesis_text=thesis_text,
        )
        logger.info(
            "ENTER %s — p=%.2f conf=%.2f checks=%d/4",
            market.polymarket_id,
            probability,
            confidence,
            checks.passed_count,
        )
        return thesis

    def _evaluate_via_crypto_quant(
        self, market: MarketData, market_db_id: UUID
    ) -> Thesis | None:
        """Deterministic Φ(d₂) pipeline for crypto-strike markets.

        Pre-condition: caller has already confirmed the question matches a
        supported pattern, so the LLM must NOT be invoked from here even
        when no thesis is produced (spot feed not ready, edge too thin).
        """
        result = self._crypto_quant.evaluate(
            question=market.question,
            hours_to_resolution=market.hours_to_resolution,
        )
        if result is None:
            logger.info(
                "REJECT %s — crypto_quant: spot feed not ready",
                market.polymarket_id,
            )
            return None

        strike, quant_result, thesis_text = result
        market_price = float(market.midpoint_price)
        edge = abs(quant_result.probability - market_price)
        if edge < self._min_edge:
            logger.info(
                "REJECT %s — crypto_quant edge %.4f < %.4f (p=%.4f price=%.4f)",
                market.polymarket_id,
                edge,
                self._min_edge,
                quant_result.probability,
                market_price,
            )
            return None

        # All four "checks" set true — the quant pipeline is the authority
        # for these markets and the legacy LLM checks don't apply.
        checks = ThesisChecks(base_rate=True, news=True, whale=True, disposition=True)
        thesis = Thesis.create(
            market_id=market_db_id,
            claude_estimate=quant_result.probability,
            confidence=quant_result.confidence,
            checks=checks,
            thesis_text=thesis_text,
        )
        logger.info(
            "ENTER %s — crypto_quant p=%.4f conf=%.2f strike=%s",
            market.polymarket_id,
            quant_result.probability,
            quant_result.confidence,
            strike,
        )
        return thesis

    def _format_rag_context(self, similar_outcomes: list[dict]) -> str:
        """Format historical outcomes for the LLM's context."""
        if not similar_outcomes:
            return "No similar historical markets found."
        lines = []
        for outcome in similar_outcomes:
            lines.append(
                f"- \"{outcome.get('question', 'N/A')}\" resolved "
                f"{outcome.get('outcome', 'N/A')} at "
                f"{outcome.get('final_price', 'N/A')}"
            )
        return "\n".join(lines)

    def _get_whale_context(self, market: MarketData) -> str:
        """Check if target wallets are active in this market."""
        return "No whale activity data available for this market."

"""Embedding generation and similarity search."""
from __future__ import annotations

import logging
import math

import voyageai

logger = logging.getLogger("polyagent.services.embeddings")


class EmbeddingsService:
    """Generates embeddings via Voyage AI and computes similarity."""

    def __init__(self, api_key: str | None = None, model: str = "voyage-3.5-lite") -> None:
        self._client = voyageai.Client(api_key=api_key) if api_key else voyageai.Client()
        self._model = model

    def embed_text(self, text: str) -> list[float]:
        """Generate an embedding for a single text.

        Args:
            text: The text to embed.

        Returns:
            A list of floats representing the embedding vector.
        """
        result = self._client.embed([text], model=self._model)
        return result.embeddings[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a batch of texts.

        Args:
            texts: List of texts to embed.

        Returns:
            A list of embedding vectors, one per input text.
        """
        if not texts:
            return []
        result = self._client.embed(texts, model=self._model)
        return result.embeddings

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors.

        Args:
            a: First embedding vector.
            b: Second embedding vector.

        Returns:
            Cosine similarity in range [0.0, 1.0]. Returns 0.0 for zero vectors.
        """
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

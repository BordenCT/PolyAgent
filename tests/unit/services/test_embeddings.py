"""Tests for embeddings service."""
from unittest.mock import MagicMock

from polyagent.services.embeddings import EmbeddingsService


class TestEmbeddingsService:
    def setup_method(self):
        self.mock_voyage = MagicMock()
        self.service = EmbeddingsService.__new__(EmbeddingsService)
        self.service._client = self.mock_voyage
        self.service._model = "voyage-3.5-lite"

    def test_embed_text_returns_list(self):
        self.mock_voyage.embed.return_value = MagicMock(
            embeddings=[[0.1] * 1024]
        )
        result = self.service.embed_text("Will BTC hit 150k?")
        assert len(result) == 1024
        assert all(isinstance(x, float) for x in result)

    def test_embed_batch_returns_multiple(self):
        self.mock_voyage.embed.return_value = MagicMock(
            embeddings=[[0.1] * 1024, [0.2] * 1024]
        )
        results = self.service.embed_batch(["q1?", "q2?"])
        assert len(results) == 2
        assert len(results[0]) == 1024

    def test_cosine_similarity(self):
        a = [1.0, 0.0, 0.0]
        b = [1.0, 0.0, 0.0]
        assert EmbeddingsService.cosine_similarity(a, b) == 1.0

    def test_cosine_similarity_orthogonal(self):
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert EmbeddingsService.cosine_similarity(a, b) == 0.0

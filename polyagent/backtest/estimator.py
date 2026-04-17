"""Pluggable probability estimators for backtesting."""
from __future__ import annotations

from abc import ABC, abstractmethod


class BaseEstimator(ABC):
    """Interface for backtest probability estimators."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def estimate(self, market_id: str, **kwargs) -> float: ...


class HistoricalEstimator(BaseEstimator):
    """Uses actual resolution outcome as probability.

    This represents the theoretical ceiling — perfect foresight.
    Useful for measuring how much of the available alpha the strategy captures.
    """

    name = "historical"

    def estimate(self, market_id: str, **kwargs) -> float:
        outcome = kwargs.get("outcome", "")
        final_price = kwargs.get("final_price", 0.5)

        if outcome == "Yes":
            return float(final_price) if final_price else 1.0
        elif outcome == "No":
            return float(final_price) if final_price else 0.0
        return float(final_price) if final_price else 0.5


class MidpointEstimator(BaseEstimator):
    """Uses market midpoint as probability estimate.

    Sanity check: should produce ~0 P&L since there's no edge.
    """

    name = "midpoint"

    def estimate(self, market_id: str, **kwargs) -> float:
        return float(kwargs.get("market_price", 0.5))


class OllamaEstimator(BaseEstimator):
    """Uses local Ollama (phi4:14b) for probability estimates.

    Zero cost, runs against the local LXC at 192.168.1.56.
    Slower than cached estimators but free and uses actual LLM reasoning.
    """

    name = "ollama"

    def __init__(self, ollama_url: str = "http://192.168.1.56:11434", model: str = "phi4:14b") -> None:
        from polyagent.data.clients.ollama import OllamaClient
        self._client = OllamaClient(base_url=ollama_url, model=model)

    def estimate(self, market_id: str, **kwargs) -> float:
        question = kwargs.get("question", "")
        market_price = float(kwargs.get("market_price", 0.5))
        if not question:
            # No question text available — can't ask LLM, fall back to
            # a slight random offset from midpoint to simulate uncertainty
            return market_price
        return self._client.estimate_probability(
            question, context=f"Current market price: {market_price:.4f}"
        )


class CachedClaudeEstimator(BaseEstimator):
    """Uses pre-computed Claude probability estimates from a cache file.

    Run `polyagent backtest --build-cache` first to generate the cache.
    """

    name = "cached-claude"

    def __init__(self, cache: dict[str, float] | None = None) -> None:
        self._cache = cache or {}

    def load_cache(self, cache: dict[str, float]) -> None:
        self._cache = cache

    def estimate(self, market_id: str, **kwargs) -> float:
        if market_id in self._cache:
            return self._cache[market_id]
        # Fallback to midpoint if not cached
        return float(kwargs.get("market_price", 0.5))

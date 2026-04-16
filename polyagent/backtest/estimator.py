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

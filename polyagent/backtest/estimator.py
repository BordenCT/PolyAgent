"""Pluggable probability estimators for backtesting."""
from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger("polyagent.backtest.estimator")


class BaseEstimator(ABC):
    """Interface for backtest probability estimators."""

    is_lookahead: bool = False  # True only for estimators that use future outcome data

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
    is_lookahead = True

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

    Answers are cached in-memory and persisted to disk (keyed by model) so
    markets that recur across days and across re-runs are answered only once.
    """

    name = "ollama"

    FLUSH_EVERY = 25

    def __init__(
        self,
        ollama_url: str = "http://192.168.1.56:11434",
        model: str = "phi4:14b",
        cache_path: str | Path | None = None,
    ) -> None:
        from polyagent.data.clients.ollama import OllamaClient
        self._client = OllamaClient(base_url=ollama_url, model=model)
        self._model = model
        self._cache_path = Path(cache_path) if cache_path else _default_cache_path(model)
        self._cache: dict[str, float] = _load_cache(self._cache_path)
        self._writes_since_flush = 0
        if self._cache:
            logger.info(
                "OllamaEstimator cache loaded: %d entries from %s",
                len(self._cache), self._cache_path,
            )

    def estimate(self, market_id: str, **kwargs) -> float:
        if market_id in self._cache:
            return self._cache[market_id]

        question = kwargs.get("question", "")
        market_price = float(kwargs.get("market_price", 0.5))
        if not question:
            return market_price

        prob = self._client.estimate_probability(
            question, context=f"Current market price: {market_price:.4f}"
        )
        self._cache[market_id] = prob
        self._writes_since_flush += 1
        if self._writes_since_flush >= self.FLUSH_EVERY:
            self._flush()
        return prob

    def flush(self) -> None:
        """Persist the cache to disk. Safe to call multiple times."""
        self._flush()

    def _flush(self) -> None:
        if not self._cache:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps(self._cache))
            self._writes_since_flush = 0
        except OSError as e:
            logger.warning("Failed to persist ollama cache to %s: %s", self._cache_path, e)

    def __del__(self) -> None:
        try:
            self._flush()
        except Exception:
            pass


def _default_cache_path(model: str) -> Path:
    """Compute the default on-disk cache path for a given model."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", model)
    return Path.home() / ".polyagent" / f"ollama_backtest_cache_{safe}.json"


def _load_cache(path: Path) -> dict[str, float]:
    """Load a cache from disk, tolerating missing/corrupt files."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return {str(k): float(v) for k, v in data.items()}
    except (OSError, ValueError, TypeError) as e:
        logger.warning("Failed to load ollama cache from %s: %s", path, e)
    return {}


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

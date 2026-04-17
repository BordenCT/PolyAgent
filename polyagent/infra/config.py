"""Application configuration from environment variables."""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _env_str(key: str, default: str | None = None) -> str:
    val = os.environ.get(key, default)
    if val is None:
        raise ValueError(f"Missing required environment variable: {key}")
    return val


def _env_int(key: str, default: int | None = None) -> int | None:
    val = os.environ.get(key, "")
    if not val:
        return default
    return int(val)


def _env_float(key: str, default: float) -> float:
    return float(os.environ.get(key, str(default)))


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, str(default)).lower()
    return val in ("true", "1", "yes")


@dataclass(frozen=True)
class Settings:
    """Immutable application settings loaded from environment variables."""

    paper_trade: bool
    scan_interval_hours: int
    scan_market_limit: int
    min_gap: float
    min_depth: float
    min_hours: float
    max_hours: float
    anthropic_api_key: str
    brain_confidence_threshold: float
    brain_min_checks: int
    kelly_max_fraction: float
    bankroll: float
    exit_target_pct: float
    exit_volume_multiplier: float
    exit_stale_hours: float
    exit_stale_threshold: float
    scanner_workers: int | None
    brain_workers: int | None
    executor_workers: int | None
    exit_workers: int | None
    database_url: str
    voyage_api_key: str | None
    polymarket_api_url: str

    # LLM provider routing
    llm_provider: str  # "claude", "ollama", or "hybrid" (ollama scanner + claude brain)

    # Ollama (local LLM)
    ollama_url: str
    ollama_model: str
    ollama_enabled: bool

    # Live order placement — belt-and-suspenders safety gate on top of paper_trade
    polymarket_live_enabled: bool

    @staticmethod
    def from_env() -> Settings:
        """Load settings from .env file and environment variables.

        .env is loaded with override=False so real env vars take precedence.
        Raises ValueError if ANTHROPIC_API_KEY is not set.
        """
        # Walk up from cwd to find .env
        env_path = Path.cwd() / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)

        return Settings(
            paper_trade=_env_bool("PAPER_TRADE", True),
            scan_interval_hours=_env_int("SCAN_INTERVAL_HOURS", 4),
            scan_market_limit=_env_int("SCAN_MARKET_LIMIT", 500),
            min_gap=_env_float("MIN_GAP", 0.07),
            min_depth=_env_float("MIN_DEPTH", 500.0),
            min_hours=_env_float("MIN_HOURS", 4.0),
            max_hours=_env_float("MAX_HOURS", 168.0),
            anthropic_api_key=_env_str("ANTHROPIC_API_KEY", ""),
            brain_confidence_threshold=_env_float("BRAIN_CONFIDENCE_THRESHOLD", 0.75),
            brain_min_checks=_env_int("BRAIN_MIN_CHECKS", 3),
            kelly_max_fraction=_env_float("KELLY_MAX_FRACTION", 0.25),
            bankroll=_env_float("BANKROLL", 800.0),
            exit_target_pct=_env_float("EXIT_TARGET_PCT", 0.85),
            exit_volume_multiplier=_env_float("EXIT_VOLUME_MULTIPLIER", 3.0),
            exit_stale_hours=_env_float("EXIT_STALE_HOURS", 24.0),
            exit_stale_threshold=_env_float("EXIT_STALE_THRESHOLD", 0.02),
            scanner_workers=_env_int("SCANNER_WORKERS"),
            brain_workers=_env_int("BRAIN_WORKERS"),
            executor_workers=_env_int("EXECUTOR_WORKERS"),
            exit_workers=_env_int("EXIT_WORKERS"),
            database_url=_env_str(
                "DATABASE_URL",
                "postgresql://polyagent:polyagent@polyagent-db:5432/polyagent",
            ),
            voyage_api_key=os.environ.get("VOYAGE_API_KEY"),
            polymarket_api_url=_env_str(
                "POLYMARKET_API_URL", "https://clob.polymarket.com"
            ),
            llm_provider=_env_str("LLM_PROVIDER", "ollama"),
            ollama_url=_env_str("OLLAMA_URL", "http://192.168.1.56:11434"),
            ollama_model=_env_str("OLLAMA_MODEL", "phi4:14b"),
            ollama_enabled=_env_bool("OLLAMA_ENABLED", True),
            polymarket_live_enabled=_env_bool("POLYMARKET_LIVE_ENABLED", False),
        )

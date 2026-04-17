"""Tests for configuration loading."""
import os
from unittest.mock import patch
from polyagent.infra.config import Settings


class TestSettings:
    def test_defaults(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False):
            s = Settings.from_env()
            assert s.paper_trade is True
            assert s.scan_interval_hours == 4
            assert s.scan_market_limit == 500
            assert s.min_gap == 0.07
            assert s.min_depth == 500.0
            assert s.min_hours == 4.0
            assert s.max_hours == 168.0
            assert s.kelly_max_fraction == 0.25
            assert s.bankroll == 800.0
            assert s.exit_target_pct == 0.85
            assert s.exit_volume_multiplier == 3.0
            assert s.exit_stale_hours == 24.0
            assert s.exit_stale_threshold == 0.02

    def test_override_from_env(self):
        overrides = {
            "ANTHROPIC_API_KEY": "sk-test", "PAPER_TRADE": "false",
            "SCAN_INTERVAL_HOURS": "1", "BANKROLL": "5000", "SCANNER_WORKERS": "32",
        }
        with patch.dict(os.environ, overrides, clear=False):
            s = Settings.from_env()
            assert s.paper_trade is False
            assert s.scan_interval_hours == 1
            assert s.bankroll == 5000.0
            assert s.scanner_workers == 32

    def test_auto_scale_workers_none(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False):
            s = Settings.from_env()
            assert s.scanner_workers is None
            assert s.brain_workers is None

    def test_missing_api_key_defaults_empty(self):
        """API key defaults to empty when using Ollama as provider."""
        with patch.dict(os.environ, {}, clear=True):
            s = Settings.from_env()
            assert s.anthropic_api_key == ""
            assert s.llm_provider == "ollama"

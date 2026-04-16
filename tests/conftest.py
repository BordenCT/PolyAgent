"""Shared test fixtures."""
from __future__ import annotations
import os
from unittest.mock import patch
import pytest
from polyagent.infra.config import Settings

@pytest.fixture
def settings() -> Settings:
    overrides = {
        "ANTHROPIC_API_KEY": "sk-test-key",
        "PAPER_TRADE": "true",
        "DATABASE_URL": "postgresql://polyagent:polyagent@localhost:5432/polyagent_test",
    }
    with patch.dict(os.environ, overrides, clear=False):
        return Settings.from_env()

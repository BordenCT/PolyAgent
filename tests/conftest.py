"""Shared test fixtures."""
from __future__ import annotations
import os
from unittest.mock import patch
import pytest
from polyagent.infra.config import Settings


def pytest_collection_modifyitems(config, items):
    """Auto-skip integration tests unless --run-integration is passed."""
    if not config.getoption("--run-integration", default=False):
        skip_integration = pytest.mark.skip(reason="needs --run-integration flag and running DB")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip_integration)


def pytest_addoption(parser):
    parser.addoption("--run-integration", action="store_true", default=False, help="Run integration tests")


@pytest.fixture
def settings() -> Settings:
    overrides = {
        "ANTHROPIC_API_KEY": "sk-test-key",
        "PAPER_TRADE": "true",
        "DATABASE_URL": "postgresql://polyagent:polyagent@localhost:5432/polyagent_test",
    }
    with patch.dict(os.environ, overrides, clear=False):
        return Settings.from_env()

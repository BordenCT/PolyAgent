"""Shared test fixtures."""
from __future__ import annotations
import os
import uuid
from unittest.mock import patch

import psycopg
import pytest

from polyagent.infra.config import Settings


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """Prevent .env files from leaking into unit tests."""
    monkeypatch.setattr("polyagent.infra.config.load_dotenv", lambda *a, **kw: None)


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


@pytest.fixture
def empty_db_url():
    """Yield a connection URL to a fresh empty database; drop on teardown.

    Requires POLYAGENT_TEST_DB_URL env var pointing at a Postgres superuser
    URL on a server we can CREATE/DROP DATABASE against.
    """
    base = os.environ.get("POLYAGENT_TEST_DB_URL")
    if not base:
        pytest.skip("POLYAGENT_TEST_DB_URL not set")
    db_name = f"polyagent_test_{uuid.uuid4().hex[:12]}"
    admin = psycopg.connect(base, autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{db_name}"')
        target_url = base.rsplit("/", 1)[0] + f"/{db_name}"
        with psycopg.connect(target_url) as conn:
            with conn.cursor() as cur:
                cur.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
            conn.commit()
        yield target_url
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        admin.close()

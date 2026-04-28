"""Integration tests for the hand-rolled SQL migration runner."""
from __future__ import annotations

import pytest
import psycopg

from polyagent.scripts.migrate import ensure_schema_migrations_table


@pytest.mark.integration
def test_ensure_schema_migrations_table_creates_table_when_absent(empty_db_url):
    with psycopg.connect(empty_db_url) as conn:
        ensure_schema_migrations_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'schema_migrations' ORDER BY ordinal_position"
            )
            cols = [r[0] for r in cur.fetchall()]
    assert cols == ["version", "filename", "checksum", "applied_at"]


@pytest.mark.integration
def test_ensure_schema_migrations_table_is_idempotent(empty_db_url):
    with psycopg.connect(empty_db_url) as conn:
        ensure_schema_migrations_table(conn)
        ensure_schema_migrations_table(conn)  # second call must not error

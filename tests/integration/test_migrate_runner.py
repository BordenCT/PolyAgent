"""Integration tests for the hand-rolled SQL migration runner."""
from __future__ import annotations

import pytest
import psycopg

from polyagent.scripts.migrate import (
    AppliedRecord,
    ensure_schema_migrations_table,
    get_applied,
)


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


@pytest.mark.integration
def test_get_applied_returns_empty_on_fresh_db(empty_db_url):
    with psycopg.connect(empty_db_url) as conn:
        ensure_schema_migrations_table(conn)
        assert get_applied(conn) == {}


@pytest.mark.integration
def test_get_applied_reflects_inserted_rows(empty_db_url):
    with psycopg.connect(empty_db_url) as conn:
        ensure_schema_migrations_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schema_migrations (version, filename, checksum) "
                "VALUES (%s, %s, %s)",
                ("001", "001_first.sql", "abc"),
            )
        conn.commit()
        applied = get_applied(conn)
    assert "001" in applied
    assert applied["001"].filename == "001_first.sql"
    assert applied["001"].checksum == "abc"

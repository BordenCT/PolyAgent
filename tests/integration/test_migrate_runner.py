"""Integration tests for the hand-rolled SQL migration runner."""
from __future__ import annotations

import pytest
import psycopg

from polyagent.scripts.migrate import (
    AppliedRecord,
    Migration,
    apply_migration,
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


@pytest.mark.integration
def test_apply_migration_runs_sql_and_records_row(empty_db_url):
    m = Migration(
        version="100",
        filename="100_make_table.sql",
        sql="CREATE TABLE widget (id INT PRIMARY KEY);",
        checksum="deadbeef",
    )
    with psycopg.connect(empty_db_url) as conn:
        ensure_schema_migrations_table(conn)
        apply_migration(conn, m)
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'widget'")
            assert cur.fetchone() is not None
        applied = get_applied(conn)
    assert "100" in applied
    assert applied["100"].checksum == "deadbeef"


@pytest.mark.integration
def test_apply_migration_rolls_back_on_sql_error(empty_db_url):
    m = Migration(
        version="200",
        filename="200_bad.sql",
        sql="CREATE TABLE good (id INT); SELECT * FROM nonexistent;",
        checksum="abc",
    )
    with psycopg.connect(empty_db_url) as conn:
        ensure_schema_migrations_table(conn)
        with pytest.raises(psycopg.errors.UndefinedTable):
            apply_migration(conn, m)
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'good'")
            assert cur.fetchone() is None
        assert get_applied(conn) == {}

"""Integration tests for the hand-rolled SQL migration runner."""
from __future__ import annotations

import pytest
import psycopg

from polyagent.scripts.migrate import (
    AppliedRecord,
    DriftError,
    Migration,
    apply_migration,
    ensure_schema_migrations_table,
    get_applied,
    migrate_baseline,
    migrate_status,
    migrate_up,
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


@pytest.mark.integration
def test_migrate_up_applies_all_pending(empty_db_url, tmp_path):
    (tmp_path / "001_a.sql").write_text("CREATE TABLE a (id INT);")
    (tmp_path / "002_b.sql").write_text("CREATE TABLE b (id INT);")
    with psycopg.connect(empty_db_url) as conn:
        applied = migrate_up(conn, tmp_path)
    assert [m.version for m in applied] == ["001", "002"]
    with psycopg.connect(empty_db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name IN ('a', 'b') ORDER BY table_name"
        )
        names = [r[0] for r in cur.fetchall()]
    assert names == ["a", "b"]


@pytest.mark.integration
def test_migrate_up_is_noop_on_second_run(empty_db_url, tmp_path):
    (tmp_path / "001_a.sql").write_text("CREATE TABLE a (id INT);")
    with psycopg.connect(empty_db_url) as conn:
        first = migrate_up(conn, tmp_path)
        second = migrate_up(conn, tmp_path)
    assert [m.version for m in first] == ["001"]
    assert second == []


@pytest.mark.integration
def test_migrate_up_raises_drift_after_file_edit(empty_db_url, tmp_path):
    (tmp_path / "001_a.sql").write_text("CREATE TABLE a (id INT);")
    with psycopg.connect(empty_db_url) as conn:
        migrate_up(conn, tmp_path)
        (tmp_path / "001_a.sql").write_text("CREATE TABLE a (id INT, extra TEXT);")
        with pytest.raises(DriftError):
            migrate_up(conn, tmp_path)


@pytest.mark.integration
def test_migrate_baseline_records_files_without_executing(empty_db_url, tmp_path):
    (tmp_path / "001_a.sql").write_text("RAISE syntax_error_intentional;")
    with psycopg.connect(empty_db_url) as conn:
        recorded = migrate_baseline(conn, tmp_path)
        assert [m.version for m in recorded] == ["001"]
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'a'")
            assert cur.fetchone() is None
        applied = get_applied(conn)
    assert "001" in applied


@pytest.mark.integration
def test_migrate_status_lists_applied_pending_drifted(empty_db_url, tmp_path):
    (tmp_path / "001_a.sql").write_text("CREATE TABLE a (id INT);")
    (tmp_path / "002_b.sql").write_text("CREATE TABLE b (id INT);")
    with psycopg.connect(empty_db_url) as conn:
        migrate_up(conn, tmp_path)
        (tmp_path / "002_b.sql").write_text("CREATE TABLE b (id INT, x TEXT);")
        (tmp_path / "003_c.sql").write_text("CREATE TABLE c (id INT);")
        report = migrate_status(conn, tmp_path)
    assert [m.version for m in report.applied] == ["001"]
    assert [m.version for m in report.pending] == ["003"]
    assert [m.version for m in report.drifted] == ["002"]

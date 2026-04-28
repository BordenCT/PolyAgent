"""Hand-rolled SQL migration runner.

Reads `db/migrations/*.sql`, applies any not yet recorded in
`schema_migrations` in a transaction each, detects checksum drift on
already-applied files.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psycopg

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def ensure_schema_migrations_table(conn: psycopg.Connection) -> None:
    """Create the schema_migrations table if it does not exist."""
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_MIGRATIONS_DDL)
    conn.commit()


@dataclass(frozen=True)
class Migration:
    version: str       # "001", "006", etc.
    filename: str      # "001_initial_schema.sql"
    sql: str
    checksum: str      # sha256 of sql bytes, hex


def discover_migrations(directory: Path) -> list[Migration]:
    """Return all *.sql files under `directory`, lex-sorted by filename.

    Version is the filename prefix before the first underscore.

    Raises:
        FileNotFoundError: if directory does not exist.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"migrations directory not found: {directory}")
    out: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        version = path.name.split("_", 1)[0]
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        out.append(Migration(version=version, filename=path.name, sql=sql, checksum=checksum))
    return out


@dataclass(frozen=True)
class AppliedRecord:
    version: str
    filename: str
    checksum: str
    applied_at: datetime


def get_applied(conn: psycopg.Connection) -> dict[str, AppliedRecord]:
    """Return {version: AppliedRecord} for all rows in schema_migrations."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version, filename, checksum, applied_at FROM schema_migrations"
        )
        rows = cur.fetchall()
    return {
        r[0]: AppliedRecord(version=r[0], filename=r[1], checksum=r[2], applied_at=r[3])
        for r in rows
    }


def apply_migration(conn: psycopg.Connection, m: Migration) -> None:
    """Execute the migration in a transaction, then record it.

    On any error the transaction is rolled back and the exception re-raised.
    """
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(m.sql)
                cur.execute(
                    "INSERT INTO schema_migrations (version, filename, checksum) "
                    "VALUES (%s, %s, %s)",
                    (m.version, m.filename, m.checksum),
                )
    except Exception:
        conn.rollback()
        raise


class DriftError(RuntimeError):
    """Raised when an applied migration's checksum does not match its file."""


def plan_actions(
    found: list[Migration], applied: dict[str, AppliedRecord]
) -> list[Migration]:
    """Return migrations needing application, in order. Raise DriftError on mismatch."""
    pending: list[Migration] = []
    for m in found:
        rec = applied.get(m.version)
        if rec is None:
            pending.append(m)
            continue
        if rec.checksum != m.checksum:
            raise DriftError(
                f"checksum drift on version {m.version} ({m.filename}): "
                f"applied={rec.checksum[:12]}... file={m.checksum[:12]}..."
            )
    return pending

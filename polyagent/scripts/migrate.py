"""Hand-rolled SQL migration runner.

Reads `db/migrations/*.sql`, applies any not yet recorded in
`schema_migrations` in a transaction each, detects checksum drift on
already-applied files.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
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
        sql = path.read_text()
        version = path.name.split("_", 1)[0]
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        out.append(Migration(version=version, filename=path.name, sql=sql, checksum=checksum))
    return out

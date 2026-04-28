"""Hand-rolled SQL migration runner.

Reads `db/migrations/*.sql`, applies any not yet recorded in
`schema_migrations` in a transaction each, detects checksum drift on
already-applied files.
"""
from __future__ import annotations

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

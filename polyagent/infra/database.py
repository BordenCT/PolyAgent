"""PostgreSQL connection pool management."""
from __future__ import annotations
import logging
from contextlib import contextmanager
from typing import Generator
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from polyagent.infra.config import Settings

logger = logging.getLogger("polyagent.database")

class Database:
    def __init__(self, settings: Settings) -> None:
        self._pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=4, max_size=32,
            kwargs={"row_factory": dict_row},
        )
        logger.info("Database pool initialized", extra={"max_size": 32})

    @contextmanager
    def connection(self) -> Generator[psycopg.Connection, None, None]:
        with self._pool.connection() as conn:
            yield conn

    @contextmanager
    def cursor(self) -> Generator[psycopg.Cursor, None, None]:
        with self.connection() as conn:
            with conn.cursor() as cur:
                yield cur
            conn.commit()

    def close(self) -> None:
        self._pool.close()
        logger.info("Database pool closed")

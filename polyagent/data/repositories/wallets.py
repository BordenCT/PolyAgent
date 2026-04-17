"""Target wallets repository."""
from __future__ import annotations

import logging

from polyagent.infra.database import Database

logger = logging.getLogger("polyagent.repositories.wallets")

SELECT_ALL = """
    SELECT address, total_trades, win_rate, total_pnl
    FROM target_wallets
    ORDER BY total_pnl DESC
"""

UPSERT_WALLET = """
    INSERT INTO target_wallets (address, total_trades, win_rate, total_pnl)
    VALUES (%(address)s, %(total_trades)s, %(win_rate)s, %(total_pnl)s)
    ON CONFLICT (address) DO UPDATE SET
        total_trades = EXCLUDED.total_trades,
        win_rate = EXCLUDED.win_rate,
        total_pnl = EXCLUDED.total_pnl
"""


class WalletRepository:
    """CRUD operations for target wallets."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def get_all(self) -> list[dict]:
        """Get all target wallets sorted by PnL."""
        with self._db.cursor() as cur:
            cur.execute(SELECT_ALL)
            return cur.fetchall()

    def upsert(self, address: str, trades: int, win_rate: float, pnl: float) -> None:
        """Insert or update a target wallet."""
        with self._db.cursor() as cur:
            cur.execute(
                UPSERT_WALLET,
                {
                    "address": address,
                    "total_trades": trades,
                    "win_rate": win_rate,
                    "total_pnl": pnl,
                },
            )

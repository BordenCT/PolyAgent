"""CRUD for btc5m_markets and btc5m_trades."""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from polyagent.infra.database import Database
from polyagent.models import Btc5mMarket, Btc5mTrade

logger = logging.getLogger("polyagent.repositories.btc5m")

UPSERT_MARKET = """
    INSERT INTO btc5m_markets (
        polymarket_id, slug, token_id_yes, token_id_no,
        window_duration_s, window_start_ts, window_end_ts
    ) VALUES (
        %(polymarket_id)s, %(slug)s, %(token_id_yes)s, %(token_id_no)s,
        %(window_duration_s)s, %(window_start_ts)s, %(window_end_ts)s
    )
    ON CONFLICT (polymarket_id) DO UPDATE SET
        slug = EXCLUDED.slug
    RETURNING id
"""

INSERT_TRADE = """
    INSERT INTO btc5m_trades (
        market_id, side, fill_price_assumed, size,
        estimator_p_up, spot_at_decision, vol_at_decision, edge_at_decision
    ) VALUES (
        %(market_id)s, %(side)s, %(fill_price_assumed)s, %(size)s,
        %(estimator_p_up)s, %(spot_at_decision)s, %(vol_at_decision)s,
        %(edge_at_decision)s
    )
    RETURNING id
"""

SELECT_UNRESOLVED_PAST_END = """
    SELECT id, polymarket_id, slug, token_id_yes, token_id_no,
           window_duration_s, window_start_ts, window_end_ts,
           start_spot, end_spot, outcome
    FROM btc5m_markets
    WHERE outcome IS NULL AND window_end_ts <= %(now)s
"""

SELECT_ACTIVE = """
    SELECT id, polymarket_id, slug, token_id_yes, token_id_no,
           window_duration_s, window_start_ts, window_end_ts,
           start_spot, end_spot, outcome
    FROM btc5m_markets
    WHERE outcome IS NULL AND window_end_ts > %(now)s
"""

UPDATE_MARKET_RESOLUTION = """
    UPDATE btc5m_markets
    SET start_spot = %(start_spot)s,
        end_spot = %(end_spot)s,
        outcome = %(outcome)s,
        resolved_at = NOW()
    WHERE id = %(id)s
"""

SELECT_TRADES_FOR_MARKET = """
    SELECT id, market_id, side, fill_price_assumed, size,
           estimator_p_up, spot_at_decision, vol_at_decision,
           edge_at_decision, pnl
    FROM btc5m_trades
    WHERE market_id = %(market_id)s
"""

UPDATE_TRADE_PNL = """
    UPDATE btc5m_trades
    SET pnl = %(pnl)s, resolved_at = NOW()
    WHERE id = %(id)s
"""


class Btc5mRepository:
    """CRUD operations for btc5m_markets and btc5m_trades tables.

    Args:
        db: Database wrapper providing cursor context manager.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def upsert_market(self, market: Btc5mMarket) -> UUID:
        """Insert or update a BTC short-horizon market, returning its UUID.

        Conflicts on polymarket_id; updates slug on collision.

        Args:
            market: The Btc5mMarket dataclass to persist.

        Returns:
            The UUID of the upserted btc5m_markets row.
        """
        with self._db.cursor() as cur:
            cur.execute(UPSERT_MARKET, {
                "polymarket_id": market.polymarket_id,
                "slug": market.slug,
                "token_id_yes": market.token_id_yes,
                "token_id_no": market.token_id_no,
                "window_duration_s": market.window_duration_s,
                "window_start_ts": market.window_start_ts,
                "window_end_ts": market.window_end_ts,
            })
            return cur.fetchone()["id"]

    def insert_trade(self, trade: Btc5mTrade) -> UUID:
        """Insert a new paper trade, returning its UUID.

        ``pnl`` is intentionally omitted — left NULL until the resolver runs.

        Args:
            trade: The Btc5mTrade dataclass to persist.

        Returns:
            The UUID of the inserted btc5m_trades row.
        """
        with self._db.cursor() as cur:
            cur.execute(INSERT_TRADE, {
                "market_id": trade.market_id,
                "side": trade.side,
                "fill_price_assumed": trade.fill_price_assumed,
                "size": trade.size,
                "estimator_p_up": trade.estimator_p_up,
                "spot_at_decision": trade.spot_at_decision,
                "vol_at_decision": trade.vol_at_decision,
                "edge_at_decision": trade.edge_at_decision,
            })
            return cur.fetchone()["id"]

    def get_active_markets(self, now: datetime) -> list[dict]:
        """Return all open markets whose resolution window has not yet closed.

        Args:
            now: Current UTC timestamp used as the cutoff.

        Returns:
            List of row dicts from btc5m_markets.
        """
        with self._db.cursor() as cur:
            cur.execute(SELECT_ACTIVE, {"now": now})
            return cur.fetchall()

    def get_unresolved_markets_past_end(self, now: datetime) -> list[dict]:
        """Return markets whose window has closed but outcome is still NULL.

        Args:
            now: Current UTC timestamp used as the cutoff.

        Returns:
            List of row dicts from btc5m_markets.
        """
        with self._db.cursor() as cur:
            cur.execute(SELECT_UNRESOLVED_PAST_END, {"now": now})
            return cur.fetchall()

    def update_market_resolution(
        self,
        market_id: UUID,
        start_spot: Decimal,
        end_spot: Decimal,
        outcome: str,
    ) -> None:
        """Stamp a market with its resolution data and set resolved_at.

        Args:
            market_id: UUID of the btc5m_markets row to update.
            start_spot: Coinbase BTC/USD at window open.
            end_spot: Coinbase BTC/USD at window close.
            outcome: 'YES' if end_spot >= start_spot, else 'NO'.
        """
        with self._db.cursor() as cur:
            cur.execute(UPDATE_MARKET_RESOLUTION, {
                "id": market_id,
                "start_spot": start_spot,
                "end_spot": end_spot,
                "outcome": outcome,
            })

    def get_trades_for_market(self, market_id: UUID) -> list[dict]:
        """Return all trades linked to a given market.

        Args:
            market_id: UUID of the parent btc5m_markets row.

        Returns:
            List of row dicts from btc5m_trades.
        """
        with self._db.cursor() as cur:
            cur.execute(SELECT_TRADES_FOR_MARKET, {"market_id": market_id})
            return cur.fetchall()

    def update_trade_pnl(self, trade_id: UUID, pnl: Decimal) -> None:
        """Set the realized P&L on a trade and stamp resolved_at.

        Args:
            trade_id: UUID of the btc5m_trades row to update.
            pnl: Realized profit/loss for the trade.
        """
        with self._db.cursor() as cur:
            cur.execute(UPDATE_TRADE_PNL, {"id": trade_id, "pnl": pnl})

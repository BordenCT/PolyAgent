"""CRUD for quant_short_markets and quant_short_trades."""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from polyagent.infra.database import Database
from polyagent.models import QuantShortMarket, QuantShortTrade

logger = logging.getLogger("polyagent.repositories.quant_short")

UPSERT_MARKET = """
    INSERT INTO quant_short_markets (
        polymarket_id, slug, token_id_yes, token_id_no,
        window_duration_s, window_start_ts, window_end_ts, asset_id
    ) VALUES (
        %(polymarket_id)s, %(slug)s, %(token_id_yes)s, %(token_id_no)s,
        %(window_duration_s)s, %(window_start_ts)s, %(window_end_ts)s, %(asset_id)s
    )
    ON CONFLICT (polymarket_id) DO UPDATE SET
        slug = EXCLUDED.slug
    RETURNING id
"""

INSERT_TRADE = """
    INSERT INTO quant_short_trades (
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
           start_spot, end_spot, outcome, asset_id, price_source_id
    FROM quant_short_markets
    WHERE outcome IS NULL AND window_end_ts <= %(now)s
"""

SELECT_ACTIVE = """
    SELECT id, polymarket_id, slug, token_id_yes, token_id_no,
           window_duration_s, window_start_ts, window_end_ts,
           start_spot, end_spot, outcome, asset_id, price_source_id
    FROM quant_short_markets
    WHERE outcome IS NULL AND window_end_ts > %(now)s
"""

UPDATE_MARKET_RESOLUTION = """
    UPDATE quant_short_markets
    SET start_spot = %(start_spot)s,
        end_spot = %(end_spot)s,
        outcome = %(outcome)s,
        resolved_at = NOW(),
        price_source_id = %(price_source_id)s
    WHERE id = %(id)s
"""

SELECT_TRADES_FOR_MARKET = """
    SELECT id, market_id, side, fill_price_assumed, size,
           estimator_p_up, spot_at_decision, vol_at_decision,
           edge_at_decision, pnl
    FROM quant_short_trades
    WHERE market_id = %(market_id)s
"""

UPDATE_TRADE_PNL = """
    UPDATE quant_short_trades
    SET pnl = %(pnl)s, resolved_at = NOW()
    WHERE id = %(id)s
"""

COUNT_OPEN_TRADES_FOR_ASSET = """
    SELECT COUNT(*) AS open_count
    FROM quant_short_trades t
    JOIN quant_short_markets m ON m.id = t.market_id
    WHERE t.pnl IS NULL AND m.asset_id = %(asset_id)s
"""


class QuantShortRepository:
    """CRUD operations for quant_short_markets and quant_short_trades.

    Args:
        db: Database wrapper providing cursor context manager.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def upsert_market(self, market: QuantShortMarket) -> UUID:
        """Insert or update a short-horizon market, returning its UUID.

        Conflicts on polymarket_id; updates slug on collision.

        Args:
            market: The QuantShortMarket dataclass to persist.

        Returns:
            The UUID of the upserted quant_short_markets row.
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
                "asset_id": market.asset_id,
            })
            return cur.fetchone()["id"]

    def insert_trade(self, trade: QuantShortTrade) -> UUID:
        """Insert a new paper trade, returning its UUID.

        ``pnl`` is intentionally omitted, left NULL until the resolver runs.

        Args:
            trade: The QuantShortTrade dataclass to persist.

        Returns:
            The UUID of the inserted quant_short_trades row.
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
        """Return all open markets whose resolution window has not yet closed."""
        with self._db.cursor() as cur:
            cur.execute(SELECT_ACTIVE, {"now": now})
            return cur.fetchall()

    def get_unresolved_markets_past_end(self, now: datetime) -> list[dict]:
        """Return markets whose window has closed but outcome is still NULL."""
        with self._db.cursor() as cur:
            cur.execute(SELECT_UNRESOLVED_PAST_END, {"now": now})
            return cur.fetchall()

    def update_market_resolution(
        self,
        market_id: UUID,
        *,
        start_spot: Decimal,
        end_spot: Decimal,
        outcome: str,
        price_source_id: str,
    ) -> None:
        """Stamp a market with its resolution data and set resolved_at.

        Args:
            market_id: UUID of the row in ``quant_short_markets``.
            start_spot: Spot at ``window_start_ts``.
            end_spot: Spot at ``window_end_ts``.
            outcome: ``YES`` or ``NO``.
            price_source_id: Audit identifier for the settlement source
                (e.g. ``coinbase:BTC-USD``) so resolved markets are
                traceable to a single price feed.
        """
        with self._db.cursor() as cur:
            cur.execute(UPDATE_MARKET_RESOLUTION, {
                "id": market_id,
                "start_spot": start_spot,
                "end_spot": end_spot,
                "outcome": outcome,
                "price_source_id": price_source_id,
            })

    def get_trades_for_market(self, market_id: UUID) -> list[dict]:
        """Return all trades linked to a given market."""
        with self._db.cursor() as cur:
            cur.execute(SELECT_TRADES_FOR_MARKET, {"market_id": market_id})
            return cur.fetchall()

    def update_trade_pnl(self, trade_id: UUID, pnl: Decimal) -> None:
        """Set the realized P&L on a trade and stamp resolved_at."""
        with self._db.cursor() as cur:
            cur.execute(UPDATE_TRADE_PNL, {"id": trade_id, "pnl": pnl})

    def count_open_trades_for_asset(self, asset_id: str) -> int:
        """Return the number of unresolved paper trades for a given asset."""
        with self._db.cursor() as cur:
            cur.execute(COUNT_OPEN_TRADES_FOR_ASSET, {"asset_id": asset_id})
            row = cur.fetchone()
        return int(row["open_count"]) if row else 0

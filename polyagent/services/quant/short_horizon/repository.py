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
        estimator_p_up, spot_at_decision, vol_at_decision, edge_at_decision,
        predicted_ev, return_5m, return_15m, return_30m,
        realized_vol_5m, concurrent_with_prior
    ) VALUES (
        %(market_id)s, %(side)s, %(fill_price_assumed)s, %(size)s,
        %(estimator_p_up)s, %(spot_at_decision)s, %(vol_at_decision)s,
        %(edge_at_decision)s,
        %(predicted_ev)s, %(return_5m)s, %(return_15m)s, %(return_30m)s,
        %(realized_vol_5m)s, %(concurrent_with_prior)s
    )
    RETURNING id
"""

INSERT_REJECTION = """
    INSERT INTO quant_decider_rejections (
        polymarket_id, slug, asset_id, reason,
        abs_edge, p_up, mid, fill_price, vol, spot, extra
    ) VALUES (
        %(polymarket_id)s, %(slug)s, %(asset_id)s, %(reason)s,
        %(abs_edge)s, %(p_up)s, %(mid)s, %(fill_price)s, %(vol)s, %(spot)s,
        %(extra)s
    )
"""

COUNT_RECENT_TRADES_FOR_ASSET = """
    SELECT COUNT(*) AS n
    FROM quant_short_trades t
    JOIN quant_short_markets m ON m.id = t.market_id
    WHERE m.asset_id = %(asset_id)s
      AND t.decision_ts > NOW() - (%(seconds)s * INTERVAL '1 second')
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
    UPDATE quant_short_trades t
    SET pnl = %(pnl)s,
        resolved_at = NOW(),
        resolution_lag_s = GREATEST(
            0,
            EXTRACT(
                EPOCH FROM (NOW() - m.window_end_ts)
            )::INTEGER
        )
    FROM quant_short_markets m
    WHERE t.id = %(id)s AND m.id = t.market_id
"""

COUNT_OPEN_TRADES_FOR_ASSET = """
    SELECT COUNT(*) AS open_count
    FROM quant_short_trades t
    JOIN quant_short_markets m ON m.id = t.market_id
    WHERE t.pnl IS NULL AND m.asset_id = %(asset_id)s
"""

SET_START_SPOT = """
    UPDATE quant_short_markets
    SET start_spot = %(start_spot)s
    WHERE id = %(id)s AND start_spot IS NULL
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
                "predicted_ev": trade.predicted_ev,
                "return_5m": trade.return_5m,
                "return_15m": trade.return_15m,
                "return_30m": trade.return_30m,
                "realized_vol_5m": trade.realized_vol_5m,
                "concurrent_with_prior": trade.concurrent_with_prior,
            })
            return cur.fetchone()["id"]

    def insert_rejection(
        self,
        *,
        reason: str,
        polymarket_id: str | None = None,
        slug: str | None = None,
        asset_id: str | None = None,
        abs_edge: float | None = None,
        p_up: float | None = None,
        mid: float | None = None,
        fill_price: Decimal | None = None,
        vol: float | None = None,
        spot: Decimal | None = None,
        extra: dict | None = None,
    ) -> None:
        """Persist a single decider-rejection event for post-hoc analysis.

        Used to make the ``Markets Rejected`` counter inspectable: each
        skip path in the decider calls this with the gate's reason code
        and whichever diagnostic fields it had at the time. ``extra``
        carries any gate-specific key/value pairs that don't fit the
        common columns (limits, headroom, asset-spec values, etc.).
        """
        from json import dumps
        with self._db.cursor() as cur:
            cur.execute(INSERT_REJECTION, {
                "polymarket_id": polymarket_id,
                "slug": slug,
                "asset_id": asset_id,
                "reason": reason,
                "abs_edge": abs_edge,
                "p_up": p_up,
                "mid": mid,
                "fill_price": fill_price,
                "vol": vol,
                "spot": spot,
                "extra": dumps(extra) if extra else None,
            })

    def count_recent_trades_for_asset(self, asset_id: str, seconds: int = 60) -> int:
        """Return the number of trades on this asset within the last ``seconds``.

        Used by the decider to flag ``concurrent_with_prior`` so calibration
        analysis can separate independent samples from clustered ones.
        """
        with self._db.cursor() as cur:
            cur.execute(COUNT_RECENT_TRADES_FOR_ASSET, {
                "asset_id": asset_id, "seconds": int(seconds),
            })
            row = cur.fetchone()
        return int(row["n"]) if row else 0

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

    def set_start_spot(self, market_id: UUID, start_spot: Decimal) -> None:
        """Write start_spot on first evaluation; no-op if already set."""
        with self._db.cursor() as cur:
            cur.execute(SET_START_SPOT, {"id": market_id, "start_spot": start_spot})

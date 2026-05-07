"""Persistence for orderbook snapshots, trade prints, funding, and mark/index.

Backs the tick log defined in ``db/migrations/011_market_data_ticks.sql``.
Designed for high-volume writes from background ingestion workers:
inserts use ``execute_values`` batching where possible, and trade-print
inserts are idempotent on ``(venue, trade_id)`` so a reconnect's replay
window doesn't double-insert.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Iterable

from polyagent.data.clients.bybit import FundingPoint, MarkIndexUpdate
from polyagent.data.clients.coinbase_ws import OrderbookSnapshot, TradePrint
from polyagent.infra.database import Database

logger = logging.getLogger("polyagent.repositories.market_data")


_INSERT_ORDERBOOK = """
    INSERT INTO orderbook_snapshots (
        ts, venue, product, best_bid, best_ask, mid, spread,
        depth_levels, bid_volume_topN, ask_volume_topN, imbalance,
        bids, asks
    ) VALUES (
        %(ts)s, %(venue)s, %(product)s, %(best_bid)s, %(best_ask)s,
        %(mid)s, %(spread)s, %(depth_levels)s,
        %(bid_volume_topN)s, %(ask_volume_topN)s, %(imbalance)s,
        %(bids)s::jsonb, %(asks)s::jsonb
    )
"""

_INSERT_TRADE = """
    INSERT INTO trade_prints (
        ts, venue, product, side, price, size, trade_id
    ) VALUES (
        %(ts)s, %(venue)s, %(product)s, %(side)s, %(price)s, %(size)s, %(trade_id)s
    )
    ON CONFLICT DO NOTHING
"""

_UPSERT_FUNDING = """
    INSERT INTO funding_history (ts, venue, product, funding_rate, annualised_rate)
    VALUES (%(ts)s, %(venue)s, %(product)s, %(funding_rate)s, %(annualised_rate)s)
    ON CONFLICT (ts, venue, product) DO UPDATE
      SET funding_rate    = EXCLUDED.funding_rate,
          annualised_rate = EXCLUDED.annualised_rate
"""

_INSERT_MARK_INDEX = """
    INSERT INTO mark_index_prices (
        ts, venue, product, mark_price, index_price, last_price, basis
    ) VALUES (
        %(ts)s, %(venue)s, %(product)s, %(mark_price)s, %(index_price)s,
        %(last_price)s, %(basis)s
    )
"""


def _levels_json(levels: list[tuple[Decimal, Decimal]]) -> str:
    """Serialise top-N levels for JSONB column. Decimals -> strings to
    preserve precision; downstream readers can parse with Decimal."""
    return json.dumps([[str(p), str(s)] for p, s in levels])


class MarketDataRepository:
    """Write-side repository for venue tick data.

    Construction uses the shared :class:`Database` wrapper. All inserts
    open a fresh cursor per call; the ingestion worker batches at the
    application level (see ``services.market_data_ingestion``).
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def insert_orderbook(self, snap: OrderbookSnapshot, depth_levels: int = 10) -> None:
        """Persist one orderbook snapshot row.

        ``depth_levels`` is recorded so analytics can tell whether a row
        was captured from a 10-level or 50-level subscription.
        """
        with self._db.cursor() as cur:
            cur.execute(_INSERT_ORDERBOOK, {
                "ts": snap.ts,
                "venue": snap.venue,
                "product": snap.product,
                "best_bid": snap.best_bid,
                "best_ask": snap.best_ask,
                "mid": snap.mid,
                "spread": snap.spread,
                "depth_levels": depth_levels,
                "bid_volume_topN": snap.bid_volume,
                "ask_volume_topN": snap.ask_volume,
                "imbalance": snap.imbalance,
                "bids": _levels_json(snap.bids),
                "asks": _levels_json(snap.asks),
            })

    def insert_trade(self, tr: TradePrint) -> None:
        """Persist one trade print, dedup'd by ``(venue, trade_id)``."""
        with self._db.cursor() as cur:
            cur.execute(_INSERT_TRADE, {
                "ts": tr.ts,
                "venue": tr.venue,
                "product": tr.product,
                "side": tr.side,
                "price": tr.price,
                "size": tr.size,
                "trade_id": tr.trade_id,
            })

    def insert_mark_index(self, m: MarkIndexUpdate) -> None:
        with self._db.cursor() as cur:
            cur.execute(_INSERT_MARK_INDEX, {
                "ts": m.ts,
                "venue": m.venue,
                "product": m.product,
                "mark_price": m.mark_price,
                "index_price": m.index_price,
                "last_price": m.last_price,
                "basis": m.basis,
            })

    def upsert_funding(self, points: Iterable[FundingPoint]) -> int:
        """Upsert a batch of funding-history rows. Returns insert count."""
        n = 0
        with self._db.cursor() as cur:
            for p in points:
                cur.execute(_UPSERT_FUNDING, {
                    "ts": p.ts,
                    "venue": p.venue,
                    "product": p.product,
                    "funding_rate": p.funding_rate,
                    "annualised_rate": p.annualised_rate,
                })
                n += 1
        return n

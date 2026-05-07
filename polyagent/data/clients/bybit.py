"""Bybit V5 WebSocket and REST client (perp market data).

Connects to ``wss://stream.bybit.com/v5/public/linear`` and subscribes to
``orderbook.50.<symbol>``, ``publicTrade.<symbol>``, and ``tickers.<symbol>``
for perpetual contracts (linear USDT-margined). REST helper polls
``/v5/market/funding/history`` for periodic funding-rate refresh.

US-accessible: Bybit's public market-data endpoints serve from
``api.bybit.com`` regardless of geography. Trading is geo-restricted from
the US but read-only feeds are not. No account or API key is required for
the channels we subscribe to here.

Design mirrors :mod:`polyagent.data.clients.coinbase_ws`: same callback
shape, same OrderbookSnapshot/TradePrint dataclasses (re-imported here),
same reconnect-with-backoff loop. Two extra dataclasses cover the
perp-only signals: :class:`MarkIndexUpdate` for tickers (mark/index/last
prices and basis) and :class:`FundingPoint` for the REST funding poll.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Awaitable, Callable

import httpx
import websockets

from polyagent.data.clients.coinbase_ws import (
    OrderbookSnapshot,
    TradePrint,
    _BookSide,
)

logger = logging.getLogger("polyagent.data.clients.bybit")

_WS_URL = "wss://stream.bybit.com/v5/public/linear"
_REST_BASE = "https://api.bybit.com"


@dataclass(frozen=True)
class MarkIndexUpdate:
    """Snapshot of mark, index, and last prices for a perpetual.

    ``basis = mark - index`` is the perp-vs-spot premium, materialized
    at ingest time so analytics need not recompute it. Positive basis
    indicates perps are pricing in upward pressure (or borrow demand);
    negative indicates inverted contango.
    """
    ts: datetime
    venue: str
    product: str
    mark_price: Decimal | None
    index_price: Decimal | None
    last_price: Decimal | None
    basis: Decimal | None


@dataclass(frozen=True)
class FundingPoint:
    """A single (ts, product, funding_rate) row from the funding tape.

    ``annualised_rate`` assumes 8h funding intervals (3 per day, 365
    days). Useful for ranking regimes; the raw ``funding_rate`` is the
    authoritative number.
    """
    ts: datetime
    venue: str
    product: str
    funding_rate: Decimal
    annualised_rate: Decimal


class BybitWSClient:
    """Stateful WS client for one Bybit perpetual symbol.

    Subscribes to three topics on connect:
      * ``orderbook.50.<symbol>`` — top-50 depth (we keep all 50 internally
        and emit top-N on snapshot via :meth:`snapshot_topN`).
      * ``publicTrade.<symbol>`` — trade prints with aggressor side.
      * ``tickers.<symbol>`` — mark, index, last; updates ~1Hz.

    Args mirror :class:`CoinbaseWSClient`. ``on_mark`` is the new callback
    specific to perp tickers.
    """

    def __init__(
        self,
        symbol: str = "BTCUSDT",
        on_trade: Callable[[TradePrint], Awaitable[None]] | None = None,
        on_snapshot: Callable[[OrderbookSnapshot], Awaitable[None]] | None = None,
        on_mark: Callable[[MarkIndexUpdate], Awaitable[None]] | None = None,
        ws_factory=None,
        max_backoff_s: float = 30.0,
    ) -> None:
        self._symbol = symbol
        self._on_trade = on_trade
        self._on_snapshot = on_snapshot
        self._on_mark = on_mark
        self._ws_factory = ws_factory or websockets.connect
        self._max_backoff = max_backoff_s
        self._bids = _BookSide()
        self._asks = _BookSide()
        self._stop = asyncio.Event()

    @property
    def venue(self) -> str:
        return "bybit"

    @property
    def product(self) -> str:
        return self._symbol

    def stop(self) -> None:
        self._stop.set()

    def snapshot_topN(self, n: int = 10) -> OrderbookSnapshot:
        """Capture the current top-N book state, timestamped now (UTC)."""
        return OrderbookSnapshot(
            ts=datetime.now(timezone.utc),
            venue=self.venue,
            product=self._symbol,
            bids=self._bids.topN(n, descending=True),
            asks=self._asks.topN(n, descending=False),
        )

    async def run(self) -> None:
        """Connect, subscribe, and stream until :meth:`stop` is called."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with self._ws_factory(_WS_URL) as ws:
                    await self._subscribe(ws)
                    backoff = 1.0
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            await self.process_message(json.loads(raw))
                        except Exception:
                            logger.exception("bybit: failed to handle message")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("bybit WS error: %s; reconnecting in %.1fs", exc, backoff)
                self._reset_book()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    break
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, self._max_backoff)

    async def _subscribe(self, ws) -> None:
        await ws.send(json.dumps({
            "op": "subscribe",
            "args": [
                f"orderbook.50.{self._symbol}",
                f"publicTrade.{self._symbol}",
                f"tickers.{self._symbol}",
            ],
        }))

    async def process_message(self, msg: dict) -> None:
        """Dispatch a single decoded WS message. Public for testability."""
        topic = msg.get("topic", "")
        if topic.startswith("orderbook."):
            await self._handle_orderbook(msg)
        elif topic.startswith("publicTrade."):
            await self._handle_trades(msg)
        elif topic.startswith("tickers."):
            await self._handle_tickers(msg)

    async def _handle_orderbook(self, msg: dict) -> None:
        ev_type = msg.get("type")  # 'snapshot' or 'delta'
        data = msg.get("data") or {}
        if ev_type == "snapshot":
            self._reset_book()
        for price_str, size_str in data.get("b", []):
            self._bids.apply(Decimal(price_str), Decimal(size_str))
        for price_str, size_str in data.get("a", []):
            self._asks.apply(Decimal(price_str), Decimal(size_str))
        if ev_type == "snapshot" and self._on_snapshot is not None:
            await self._on_snapshot(self.snapshot_topN())

    async def _handle_trades(self, msg: dict) -> None:
        if self._on_trade is None:
            return
        for tr in msg.get("data", []):
            # Bybit ``S`` field: 'Buy' or 'Sell' for aggressor side.
            side = (tr.get("S") or "").lower()
            if side not in ("buy", "sell"):
                continue
            ts_ms = int(tr.get("T", 0))
            await self._on_trade(TradePrint(
                ts=datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc),
                venue=self.venue,
                product=tr.get("s") or self._symbol,
                side=side,
                price=Decimal(str(tr.get("p", "0"))),
                size=Decimal(str(tr.get("v", "0"))),
                trade_id=tr.get("i"),
            ))

    async def _handle_tickers(self, msg: dict) -> None:
        if self._on_mark is None:
            return
        data = msg.get("data") or {}
        ts_ms = int(msg.get("ts", 0))
        ts = (datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
              if ts_ms else datetime.now(timezone.utc))
        mark = _maybe_decimal(data.get("markPrice"))
        index = _maybe_decimal(data.get("indexPrice"))
        last = _maybe_decimal(data.get("lastPrice"))
        basis = (mark - index) if (mark is not None and index is not None) else None
        await self._on_mark(MarkIndexUpdate(
            ts=ts,
            venue=self.venue,
            product=data.get("symbol") or self._symbol,
            mark_price=mark,
            index_price=index,
            last_price=last,
            basis=basis,
        ))

    def _reset_book(self) -> None:
        self._bids = _BookSide()
        self._asks = _BookSide()


def _maybe_decimal(raw) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except Exception:
        return None


async def fetch_funding_history(
    symbol: str = "BTCUSDT",
    limit: int = 200,
    http_client: httpx.AsyncClient | None = None,
) -> list[FundingPoint]:
    """Pull recent funding-rate history for a Bybit perpetual.

    Bybit funds every 8h. ``limit=200`` gives roughly the last 67 days.
    Caller is responsible for upsert-by-(ts, product) to avoid duplicates
    on subsequent polls.
    """
    own_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.get(
            f"{_REST_BASE}/v5/market/funding/history",
            params={"category": "linear", "symbol": symbol, "limit": limit},
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("retCode") != 0:
            logger.warning("bybit funding non-zero retCode: %s", body)
            return []
        out: list[FundingPoint] = []
        for row in body.get("result", {}).get("list", []):
            try:
                rate = Decimal(str(row["fundingRate"]))
                ts_ms = int(row["fundingRateTimestamp"])
                out.append(FundingPoint(
                    ts=datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc),
                    venue="bybit",
                    product=row.get("symbol", symbol),
                    funding_rate=rate,
                    # 8h funding * 3/day * 365 days = 1095 periods/year.
                    annualised_rate=rate * Decimal("1095"),
                ))
            except (KeyError, ValueError, TypeError):
                continue
        return out
    finally:
        if own_client:
            await client.aclose()

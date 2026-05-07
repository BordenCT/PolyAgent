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
# Symmetric with Coinbase: 10MB cap so initial orderbook.50 snapshots and
# bursty deltas during high-vol regimes don't trip the 1MB default.
_WS_MAX_FRAME_BYTES = 10 * 1024 * 1024


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
        on_funding: Callable[[FundingPoint], Awaitable[None]] | None = None,
        ws_factory=None,
        max_backoff_s: float = 30.0,
    ) -> None:
        self._symbol = symbol
        self._on_trade = on_trade
        self._on_snapshot = on_snapshot
        self._on_mark = on_mark
        self._on_funding = on_funding
        self._ws_factory = ws_factory or websockets.connect
        self._max_backoff = max_backoff_s
        self._bids = _BookSide()
        self._asks = _BookSide()
        self._stop = asyncio.Event()
        # Bybit ticker emits *deltas* — each message carries only the
        # fields that changed since the last update. We merge incoming
        # deltas into this running state so every emit downstream has
        # the full mark/index/last picture (and a non-NULL basis whenever
        # both mark and index have ever been seen).
        self._last_mark: Decimal | None = None
        self._last_index: Decimal | None = None
        self._last_lastp: Decimal | None = None
        # Funding state. Bybit's REST funding-history endpoint geo-blocks
        # US IPs (403), but every ticker WS message carries the current
        # fundingRate field. We snapshot it into funding_history whenever
        # it changes (rate locks in once per 8h cycle).
        self._last_funding_rate: Decimal | None = None

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
                async with self._ws_factory(
                    _WS_URL, max_size=_WS_MAX_FRAME_BYTES,
                ) as ws:
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
        data = msg.get("data") or {}
        ts_ms = int(msg.get("ts", 0))
        ts = (datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
              if ts_ms else datetime.now(timezone.utc))
        product = data.get("symbol") or self._symbol

        # Merge mark/index/last deltas into running state; only update
        # fields the message actually carries. This ensures every emit
        # has the full latest picture and basis is computable as soon as
        # both legs have arrived.
        new_mark = _maybe_decimal(data.get("markPrice"))
        new_index = _maybe_decimal(data.get("indexPrice"))
        new_last = _maybe_decimal(data.get("lastPrice"))
        if new_mark is not None:
            self._last_mark = new_mark
        if new_index is not None:
            self._last_index = new_index
        if new_last is not None:
            self._last_lastp = new_last

        if self._on_mark is not None:
            basis = (
                self._last_mark - self._last_index
                if self._last_mark is not None and self._last_index is not None
                else None
            )
            await self._on_mark(MarkIndexUpdate(
                ts=ts,
                venue=self.venue,
                product=product,
                mark_price=self._last_mark,
                index_price=self._last_index,
                last_price=self._last_lastp,
                basis=basis,
            ))

        # Funding rate. Tickers carry the *current* (next-payment) funding
        # rate; it changes infrequently (locks in toward end of cycle).
        # Emit only when the rate actually changes so funding_history
        # records one row per distinct rate observation.
        if self._on_funding is not None:
            rate = _maybe_decimal(data.get("fundingRate"))
            if rate is not None and rate != self._last_funding_rate:
                self._last_funding_rate = rate
                await self._on_funding(FundingPoint(
                    ts=ts,
                    venue=self.venue,
                    product=product,
                    funding_rate=rate,
                    # 8h funding * 3/day * 365 = 1095 periods/year.
                    annualised_rate=rate * Decimal("1095"),
                ))

    def _reset_book(self) -> None:
        self._bids = _BookSide()
        self._asks = _BookSide()
        # Drop merged ticker state too. On reconnect Bybit sends a
        # snapshot ticker first, so the running state will be repopulated
        # before the next emit; clearing prevents stale-by-disconnect basis.
        self._last_mark = None
        self._last_index = None
        self._last_lastp = None
        # Don't reset _last_funding_rate: funding only changes at funding
        # cycle close, and we want to capture rate at the next cycle even
        # if it carries over identical across the reconnect.


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

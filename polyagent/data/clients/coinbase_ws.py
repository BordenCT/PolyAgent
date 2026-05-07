"""Coinbase Advanced Trade WebSocket client.

Connects to ``wss://advanced-trade-ws.coinbase.com``, subscribes to the
public ``level2`` and ``market_trades`` channels, maintains a local
order-book, and emits normalized snapshots and trade prints to caller
callbacks. Public market data; no auth required.

Design:
    * The WS feed is fully async (websockets library). The client exposes
      ``run()`` as a coroutine the caller can run in a worker.
    * Order-book is rebuilt from the initial snapshot and mutated in
      place by subsequent updates. ``snapshot_topN`` returns the current
      top-N levels on demand for time-sliced persistence.
    * Reconnect on disconnect with exponential backoff. State is
      reset on reconnect because Coinbase replays a fresh snapshot.
    * Designed for testability: the WS factory is injectable so unit
      tests can drive the client with a fake stream of messages.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Awaitable, Callable

import websockets

logger = logging.getLogger("polyagent.data.clients.coinbase_ws")

_WS_URL = "wss://advanced-trade-ws.coinbase.com"


@dataclass(frozen=True)
class OrderbookSnapshot:
    """Top-N depth snapshot at a moment in time.

    ``bids`` and ``asks`` are lists of ``(price, size)`` pairs ordered
    best-first (highest bid first; lowest ask first).
    """
    ts: datetime
    venue: str
    product: str
    bids: list[tuple[Decimal, Decimal]]
    asks: list[tuple[Decimal, Decimal]]

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / Decimal("2")

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def bid_volume(self) -> Decimal:
        return sum((q for _, q in self.bids), Decimal("0"))

    @property
    def ask_volume(self) -> Decimal:
        return sum((q for _, q in self.asks), Decimal("0"))

    @property
    def imbalance(self) -> Decimal | None:
        bv, av = self.bid_volume, self.ask_volume
        total = bv + av
        if total == 0:
            return None
        return (bv - av) / total


@dataclass(frozen=True)
class TradePrint:
    """A single trade fill from the venue's tape."""
    ts: datetime
    venue: str
    product: str
    side: str           # 'buy' or 'sell' (aggressor)
    price: Decimal
    size: Decimal
    trade_id: str | None = None


@dataclass
class _BookSide:
    """Mutable side of the local order-book.

    Stores ``{price: size}`` and emits a sorted top-N view on demand.
    Coinbase L2 updates use ``new_quantity == "0"`` to delete a level.
    """
    levels: dict[Decimal, Decimal] = field(default_factory=dict)

    def apply(self, price: Decimal, size: Decimal) -> None:
        if size == 0:
            self.levels.pop(price, None)
        else:
            self.levels[price] = size

    def topN(self, n: int, descending: bool) -> list[tuple[Decimal, Decimal]]:
        items = sorted(self.levels.items(), key=lambda kv: kv[0], reverse=descending)
        return items[:n]


class CoinbaseWSClient:
    """Stateful WS client for one Coinbase product.

    Args:
        product_id: Coinbase product (e.g. ``"BTC-USD"``).
        on_trade: Optional async callback invoked with each TradePrint.
        on_snapshot: Optional async callback invoked when the local book
            is rebuilt from a fresh L2 snapshot. Fine-grained updates do
            not fire this; use :meth:`snapshot_topN` for time-sliced reads.
        ws_factory: Optional injected WebSocket factory for tests. The
            factory must be an async-context-manager that yields a
            connection with ``send`` and ``__aiter__``.
        max_backoff_s: Cap on exponential reconnect delay.
    """

    def __init__(
        self,
        product_id: str = "BTC-USD",
        on_trade: Callable[[TradePrint], Awaitable[None]] | None = None,
        on_snapshot: Callable[[OrderbookSnapshot], Awaitable[None]] | None = None,
        ws_factory=None,
        max_backoff_s: float = 30.0,
    ) -> None:
        self._product = product_id
        self._on_trade = on_trade
        self._on_snapshot = on_snapshot
        self._ws_factory = ws_factory or websockets.connect
        self._max_backoff = max_backoff_s
        self._bids = _BookSide()
        self._asks = _BookSide()
        self._stop = asyncio.Event()

    @property
    def venue(self) -> str:
        return "coinbase"

    @property
    def product(self) -> str:
        return self._product

    def stop(self) -> None:
        """Signal the run loop to exit at the next iteration."""
        self._stop.set()

    def snapshot_topN(self, n: int = 10) -> OrderbookSnapshot:
        """Capture the current top-N book state.

        Returns an :class:`OrderbookSnapshot` with timestamp = now UTC.
        Safe to call from any thread/coroutine; reads dict copies.
        """
        return OrderbookSnapshot(
            ts=datetime.now(timezone.utc),
            venue=self.venue,
            product=self._product,
            bids=self._bids.topN(n, descending=True),
            asks=self._asks.topN(n, descending=False),
        )

    async def run(self) -> None:
        """Connect, subscribe, and stream until :meth:`stop` is called.

        Reconnects on any exception with exponential backoff. State is
        cleared on reconnect since Coinbase will replay a fresh snapshot.
        """
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
                            logger.exception("coinbase: failed to handle message")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("coinbase WS error: %s; reconnecting in %.1fs", exc, backoff)
                self._reset_book()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    break  # stop fired during backoff
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, self._max_backoff)

    async def _subscribe(self, ws) -> None:
        for channel in ("level2", "market_trades"):
            await ws.send(json.dumps({
                "type": "subscribe",
                "channel": channel,
                "product_ids": [self._product],
            }))

    async def process_message(self, msg: dict) -> None:
        """Dispatch a single decoded WS message to its handler.

        Public so unit tests can drive the client without standing up
        the run loop / WS handshake. The run loop calls this on every
        decoded frame.
        """
        channel = msg.get("channel")
        if channel == "l2_data":
            await self._handle_l2(msg)
        elif channel == "market_trades":
            await self._handle_trades(msg)
        # Ignore subscriptions, heartbeats, and channels we didn't ask for.

    async def _handle_l2(self, msg: dict) -> None:
        for event in msg.get("events", []):
            ev_type = event.get("type")
            if ev_type == "snapshot":
                self._reset_book()
            for upd in event.get("updates", []):
                side = upd.get("side")
                price = Decimal(upd.get("price_level", "0"))
                size = Decimal(upd.get("new_quantity", "0"))
                if side == "bid":
                    self._bids.apply(price, size)
                elif side in ("offer", "ask"):
                    self._asks.apply(price, size)
            if ev_type == "snapshot" and self._on_snapshot is not None:
                await self._on_snapshot(self.snapshot_topN())

    async def _handle_trades(self, msg: dict) -> None:
        if self._on_trade is None:
            return
        for event in msg.get("events", []):
            for tr in event.get("trades", []):
                # Coinbase reports side in upper-case ('BUY' / 'SELL'). Lower-case
                # to match our schema convention.
                ts = _parse_ts(tr.get("time"))
                side = (tr.get("side") or "").lower()
                if side not in ("buy", "sell"):
                    continue
                await self._on_trade(TradePrint(
                    ts=ts,
                    venue=self.venue,
                    product=tr.get("product_id") or self._product,
                    side=side,
                    price=Decimal(tr.get("price", "0")),
                    size=Decimal(tr.get("size", "0")),
                    trade_id=tr.get("trade_id"),
                ))

    def _reset_book(self) -> None:
        self._bids = _BookSide()
        self._asks = _BookSide()


def _parse_ts(raw: str | None) -> datetime:
    """Parse Coinbase ISO-8601 timestamps. Falls back to now() on failure."""
    if not raw:
        return datetime.now(timezone.utc)
    try:
        # Coinbase timestamps may include nanoseconds beyond what fromisoformat
        # accepts; truncate to microseconds.
        if "." in raw:
            head, frac = raw.split(".", 1)
            tz = ""
            for marker in ("Z", "+", "-"):
                idx = frac.find(marker)
                if idx >= 0:
                    tz = frac[idx:]
                    frac = frac[:idx]
                    break
            frac = (frac + "000000")[:6]
            raw = f"{head}.{frac}{tz}"
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except Exception:
        return datetime.now(timezone.utc)

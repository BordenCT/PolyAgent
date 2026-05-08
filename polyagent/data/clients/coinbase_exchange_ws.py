"""Coinbase Exchange (Pro-style) WebSocket client.

Connects to ``wss://ws-feed.exchange.coinbase.com`` and subscribes to the
public ``level2_batch`` and ``matches`` channels. This is the older
Coinbase Pro feed; it predates the Advanced Trade WS and crucially does
NOT require JWT signing for public market data, which the Advanced Trade
``level2`` and ``market_trades`` channels now do.

Same shape and callback contract as
:class:`polyagent.data.clients.coinbase_ws.CoinbaseWSClient`. Existing
:class:`OrderbookSnapshot` and :class:`TradePrint` dataclasses are
reused so the ingestion service can swap one client for the other
without touching downstream persistence.

Two semantic differences from Advanced Trade worth noting:
1. ``level2_batch`` sends snapshot once on subscribe, then ``l2update``
   messages with ``changes: [[side, price, size], ...]``. ``size == "0"``
   removes the level (same as Advanced Trade).
2. ``matches`` reports the *maker* side, not the aggressor. We invert
   so the persisted ``side`` matches the aggressor convention used
   everywhere else in the system (Bybit's ``S`` field is already the
   aggressor side).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Awaitable, Callable

import websockets

from polyagent.data.clients.coinbase_ws import (
    OrderbookSnapshot,
    TradePrint,
    _BookSide,
    _parse_ts,
)

logger = logging.getLogger("polyagent.data.clients.coinbase_exchange_ws")

_WS_URL = "wss://ws-feed.exchange.coinbase.com"
# Same headroom rationale as the Advanced Trade client: snapshots can
# approach 1 MB and burst higher in volatile regimes.
_WS_MAX_FRAME_BYTES = 10 * 1024 * 1024


class CoinbaseExchangeWSClient:
    """No-auth WS client for Coinbase Exchange spot.

    Args:
        product_id: Coinbase Exchange product (e.g. ``"BTC-USD"``).
        on_trade: Optional async callback for each TradePrint. Coinbase
            Exchange ``matches`` reports maker side; this client inverts
            to aggressor side before invoking the callback.
        on_snapshot: Optional async callback fired when the local book
            is rebuilt from a fresh L2 snapshot. Use :meth:`snapshot_topN`
            for time-sliced reads.
        ws_factory: Injected WS factory for tests. Same contract as
            :class:`websockets.connect` (must accept a URL and ``max_size``).
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
        # Same venue label as the Advanced Trade client so existing
        # analytics queries that filter by venue='coinbase' keep working
        # whether the data comes from the Pro or Advanced Trade feed.
        return "coinbase"

    @property
    def product(self) -> str:
        return self._product

    def stop(self) -> None:
        self._stop.set()

    def snapshot_topN(self, n: int = 10) -> OrderbookSnapshot:
        return OrderbookSnapshot(
            ts=datetime.now(timezone.utc),
            venue=self.venue,
            product=self._product,
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
                            logger.exception("coinbase exchange: handler error")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "coinbase exchange WS error: %s; reconnecting in %.1fs",
                    exc, backoff,
                )
                self._reset_book()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    break
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, self._max_backoff)

    async def _subscribe(self, ws) -> None:
        # Subscribe to each channel in its own request. Combining
        # ``level2_batch`` with other simple-string channels in one
        # subscribe message is unreliable on Coinbase Exchange: the
        # server accepts the request but silently drops the trailing
        # channels, leaving only level2_batch active. Splitting the
        # subscribe into per-channel sends works around it.
        for channel in ("level2_batch", "matches", "heartbeat"):
            await ws.send(json.dumps({
                "type": "subscribe",
                "product_ids": [self._product],
                "channels": [channel],
            }))

    async def process_message(self, msg: dict) -> None:
        """Dispatch a single decoded WS message. Public for testability."""
        msg_type = msg.get("type")
        if msg_type == "snapshot":
            await self._handle_snapshot(msg)
        elif msg_type == "l2update":
            await self._handle_l2update(msg)
        elif msg_type == "match" or msg_type == "last_match":
            await self._handle_match(msg)
        elif msg_type == "subscriptions":
            # Log so silently-dropped subscriptions surface in the operator
            # log instead of producing zero-data confusion downstream.
            channels = [c.get("name") for c in (msg.get("channels") or [])]
            logger.info("coinbase exchange subscribed: %s", channels)
        elif msg_type == "error":
            logger.warning("coinbase exchange WS error from server: %s", msg)
        # Ignore "heartbeat" and any other types.

    async def _handle_snapshot(self, msg: dict) -> None:
        self._reset_book()
        for price_str, size_str in msg.get("bids", []):
            self._bids.apply(Decimal(price_str), Decimal(size_str))
        for price_str, size_str in msg.get("asks", []):
            self._asks.apply(Decimal(price_str), Decimal(size_str))
        if self._on_snapshot is not None:
            await self._on_snapshot(self.snapshot_topN())

    async def _handle_l2update(self, msg: dict) -> None:
        # changes is a list of [side, price, size] triples; ``side`` is
        # 'buy' or 'sell' indicating the side of the BOOK being updated.
        for change in msg.get("changes", []):
            if len(change) < 3:
                continue
            side, price_str, size_str = change[0], change[1], change[2]
            price, size = Decimal(price_str), Decimal(size_str)
            if side == "buy":
                self._bids.apply(price, size)
            elif side == "sell":
                self._asks.apply(price, size)

    async def _handle_match(self, msg: dict) -> None:
        if self._on_trade is None:
            return
        # IMPORTANT: ``side`` in matches is the MAKER side, not the
        # aggressor. Invert before emitting so persisted rows use the
        # aggressor convention (matches Bybit's ``S`` and Advanced Trade).
        # Per Coinbase docs: side='buy' means maker bought = aggressor
        # sold (a down-tick). side='sell' means maker sold = aggressor
        # bought (an up-tick).
        maker_side = (msg.get("side") or "").lower()
        if maker_side == "buy":
            aggressor = "sell"
        elif maker_side == "sell":
            aggressor = "buy"
        else:
            return
        await self._on_trade(TradePrint(
            ts=_parse_ts(msg.get("time")),
            venue=self.venue,
            product=msg.get("product_id") or self._product,
            side=aggressor,
            price=Decimal(str(msg.get("price", "0"))),
            size=Decimal(str(msg.get("size", "0"))),
            trade_id=str(msg["trade_id"]) if "trade_id" in msg else None,
        ))

    def _reset_book(self) -> None:
        self._bids = _BookSide()
        self._asks = _BookSide()

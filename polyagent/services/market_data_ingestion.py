"""Background ingestion of crypto venue tick data into PolyAgent.

Owns two WebSocket clients (Coinbase Advanced Trade spot + Bybit V5 perp)
and persists their output into the tick log defined by migration 011.
Trades are persisted as they arrive; orderbooks are sampled at a
configurable cadence (default 1s) so write volume stays bounded.

The service is event-loop friendly. Callers spawn :meth:`run` on its
own asyncio loop in a worker thread; :meth:`stop` signals graceful
shutdown.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from threading import Thread

from polyagent.data.clients.bybit import (
    BybitWSClient,
    FundingPoint,
    MarkIndexUpdate,
    fetch_funding_history,
)
from polyagent.data.clients.coinbase_ws import (
    CoinbaseWSClient,
    OrderbookSnapshot,
    TradePrint,
)
from polyagent.data.repositories.market_data import MarketDataRepository

logger = logging.getLogger("polyagent.services.market_data_ingestion")


class MarketDataIngestionService:
    """Coordinator for venue WS clients and tick-log persistence.

    Args:
        repo: write-side repository for the tick log.
        coinbase_product: Coinbase Advanced Trade product to subscribe to
            (default ``BTC-USD``).
        bybit_symbol: Bybit V5 perp symbol (default ``BTCUSDT``).
        snapshot_interval_s: how often to capture orderbook top-N to the
            DB. Trades are persisted as they arrive regardless.
        depth_levels: how many top-N levels to persist per snapshot.
        funding_poll_s: how often to refresh Bybit funding history
            (default 4h; Bybit funds every 8h).
    """

    def __init__(
        self,
        repo: MarketDataRepository,
        coinbase_product: str = "BTC-USD",
        bybit_symbol: str = "BTCUSDT",
        snapshot_interval_s: float = 1.0,
        depth_levels: int = 10,
        funding_poll_s: float = 4 * 3600.0,
    ) -> None:
        self._repo = repo
        self._snapshot_interval = snapshot_interval_s
        self._depth_levels = depth_levels
        self._funding_poll = funding_poll_s
        self._coinbase = CoinbaseWSClient(
            product_id=coinbase_product,
            on_trade=self._on_trade,
            on_snapshot=None,  # Snapshots are persisted on the timer, not on book reset.
        )
        self._bybit = BybitWSClient(
            symbol=bybit_symbol,
            on_trade=self._on_trade,
            on_snapshot=None,
            on_mark=self._on_mark,
        )
        self._stop = asyncio.Event()
        self._loop_thread: Thread | None = None

    # ---- public lifecycle ----

    def start_in_thread(self) -> None:
        """Spawn an asyncio loop in a daemon thread and run there.

        The PolyAgent main process uses threads for its worker pool; an
        asyncio service in its own thread keeps the WS clients
        independent of the synchronous worker code.
        """
        if self._loop_thread is not None:
            return
        self._loop_thread = Thread(
            target=self._thread_main, name="market-data-ingestion", daemon=True,
        )
        self._loop_thread.start()

    def stop(self) -> None:
        """Signal both WS clients to disconnect and the loop to exit."""
        self._stop.set()
        self._coinbase.stop()
        self._bybit.stop()

    # ---- internal: asyncio loop ----

    def _thread_main(self) -> None:
        try:
            asyncio.run(self.run())
        except Exception:
            logger.exception("market data ingestion thread exited")

    async def run(self) -> None:
        """Run the full ingestion service until :meth:`stop` is signalled."""
        try:
            await asyncio.gather(
                self._coinbase.run(),
                self._bybit.run(),
                self._snapshot_loop(),
                self._funding_loop(),
            )
        except asyncio.CancelledError:
            pass

    # ---- callbacks: trade prints ----

    async def _on_trade(self, tr: TradePrint) -> None:
        """Persist each trade print as it arrives. Off-loop the DB call so
        a slow write doesn't back up the WS read pump."""
        try:
            await asyncio.to_thread(self._repo.insert_trade, tr)
        except Exception:
            logger.exception("failed to persist trade %s/%s", tr.venue, tr.trade_id)

    async def _on_mark(self, m: MarkIndexUpdate) -> None:
        """Persist Bybit mark/index/last every emit (~1Hz)."""
        try:
            await asyncio.to_thread(self._repo.insert_mark_index, m)
        except Exception:
            logger.exception("failed to persist mark/index for %s/%s", m.venue, m.product)

    # ---- timer loops ----

    async def _snapshot_loop(self) -> None:
        """Persist orderbook top-N for both venues at fixed cadence."""
        # Tiny initial pause so the first snapshot has actual book content
        # rather than the empty pre-subscribe state.
        await asyncio.sleep(min(2.0, self._snapshot_interval * 2))
        while not self._stop.is_set():
            try:
                for client in (self._coinbase, self._bybit):
                    snap = client.snapshot_topN(self._depth_levels)
                    if not snap.bids and not snap.asks:
                        continue  # book empty, nothing to save yet
                    try:
                        await asyncio.to_thread(
                            self._repo.insert_orderbook, snap, self._depth_levels,
                        )
                    except Exception:
                        logger.exception("failed to persist orderbook %s/%s",
                                         snap.venue, snap.product)
            except asyncio.CancelledError:
                raise
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._snapshot_interval)
                break
            except asyncio.TimeoutError:
                continue

    async def _funding_loop(self) -> None:
        """Periodically refresh Bybit funding history. Idempotent upserts."""
        # First poll runs immediately so historical context is loaded on
        # service start; subsequent polls obey ``funding_poll_s``.
        next_at = datetime.now(timezone.utc)
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            if now < next_at:
                wait_s = (next_at - now).total_seconds()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wait_s)
                    break
                except asyncio.TimeoutError:
                    pass
            try:
                points = await fetch_funding_history(symbol=self._bybit.product, limit=200)
                if points:
                    n = await asyncio.to_thread(self._repo.upsert_funding, points)
                    logger.info("funding refresh: %d rows upserted", n)
            except Exception:
                logger.exception("funding poll failed")
            next_at = datetime.now(timezone.utc) + timedelta(seconds=self._funding_poll)

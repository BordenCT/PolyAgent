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
from threading import Thread

from polyagent.data.clients.bybit import (
    BybitWSClient,
    FundingPoint,
    MarkIndexUpdate,
)
from polyagent.data.clients.coinbase_exchange_ws import CoinbaseExchangeWSClient
from polyagent.data.clients.coinbase_ws import OrderbookSnapshot, TradePrint
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
    ) -> None:
        self._repo = repo
        self._snapshot_interval = snapshot_interval_s
        self._depth_levels = depth_levels
        # Coinbase Exchange (Pro) feed: no JWT required for level2_batch +
        # matches. The newer Advanced Trade feed (CoinbaseWSClient) requires
        # signed subscriptions for those channels and silently drops the
        # ones we care about when called unauthenticated, which is why we
        # default to the Pro endpoint for pure data collection.
        self._coinbase = CoinbaseExchangeWSClient(
            product_id=coinbase_product,
            on_trade=self._on_trade,
            on_snapshot=None,  # Snapshots are persisted on the timer, not on book reset.
        )
        self._bybit = BybitWSClient(
            symbol=bybit_symbol,
            on_trade=self._on_trade,
            on_snapshot=None,
            on_mark=self._on_mark,
            # Funding now arrives via the WS tickers stream rather than
            # REST. Bybit geo-blocks /v5/market/funding/history from US
            # IPs (403); the WS endpoint is open and includes fundingRate
            # in every ticker message. We persist on each rate change.
            on_funding=self._on_funding,
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

    async def _on_funding(self, p: FundingPoint) -> None:
        """Persist a funding-rate observation when the WS reports a change."""
        try:
            await asyncio.to_thread(self._repo.upsert_funding, [p])
        except Exception:
            logger.exception("failed to persist funding for %s/%s", p.venue, p.product)

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


"""Single-threaded BTC 5m worker loop."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from polyagent.data.clients.polymarket import PolymarketClient
from polyagent.data.repositories.btc5m import Btc5mRepository
from polyagent.infra.config import Settings
from polyagent.services.btc5m.decider import Btc5mDecider, BookFetcher
from polyagent.services.btc5m.resolver import Btc5mResolver, SpotHistory
from polyagent.services.btc5m.scanner import Btc5mScanner
from polyagent.services.btc5m.spot import BtcSpotSource

logger = logging.getLogger("polyagent.services.btc5m.worker")


class PolymarketBookFetcher(BookFetcher):
    """BookFetcher backed by PolyAgent's existing Polymarket CLOB client."""

    def __init__(self, client: PolymarketClient) -> None:
        self._client = client

    def fetch_mid(self, token_id: str):
        try:
            book = self._client.fetch_order_book(token_id)
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if not bids or not asks:
                return None
            best_bid = Decimal(str(bids[0]["price"]))
            best_ask = Decimal(str(asks[0]["price"]))
            return best_bid, best_ask
        except Exception as exc:
            logger.warning("book fetch failed for %s: %s", token_id, exc)
            return None


class CoinbaseCandleHistory(SpotHistory):
    """Fetches BTC/USD price at a past timestamp from Coinbase 1-min candles."""

    def __init__(self) -> None:
        self._http = httpx.Client(timeout=10.0)

    def price_at(self, ts):
        start = int(ts.timestamp()) - 60
        end = int(ts.timestamp()) + 60
        try:
            resp = self._http.get(
                "https://api.exchange.coinbase.com/products/BTC-USD/candles",
                params={"granularity": 60, "start": start, "end": end},
            )
            resp.raise_for_status()
            candles = resp.json()
            target = int(ts.timestamp())
            best = None
            best_dt = None
            for c in candles:
                dt = abs(c[0] - target)
                if best_dt is None or dt < best_dt:
                    best, best_dt = c, dt
            if best is None:
                return None
            return Decimal(str(best[4]))
        except Exception as exc:
            logger.warning("coinbase candle fetch failed for ts=%s: %s", ts, exc)
            return None


def run_btc5m_worker(
    settings: Settings,
    repo: Btc5mRepository,
    polymarket: PolymarketClient,
    shutdown_q,
) -> None:
    """Long-running BTC 5m worker. Returns when shutdown_q is non-empty.

    Ticks spot price every ``settings.btc5m_spot_poll_s`` seconds. On each
    tick it also checks whether the market-poll interval has elapsed; if so,
    it runs scan → upsert → decide → resolve in sequence. All phases are
    individually guarded so one failure does not abort the others.

    Args:
        settings: Application settings (read-only).
        repo: Btc5mRepository for market and trade persistence.
        polymarket: Shared Polymarket CLOB client used for orderbook fetches.
        shutdown_q: Queue whose non-empty state signals the worker to exit.
    """
    spot = BtcSpotSource()
    scanner = Btc5mScanner()
    book = PolymarketBookFetcher(polymarket)
    history = CoinbaseCandleHistory()
    decider = Btc5mDecider(
        spot=spot,
        book=book,
        repo=repo,
        edge_threshold=settings.btc5m_edge_threshold,
        position_size_usd=Decimal(str(settings.btc5m_position_size_usd)),
        fees_bps=settings.btc5m_fees_bps,
        vol_window_s=settings.btc5m_vol_window_s,
    )
    resolver = Btc5mResolver(repo=repo, spot_history=history)

    last_market_poll = 0.0
    spot_interval = settings.btc5m_spot_poll_s
    market_interval = settings.btc5m_market_poll_s
    logger.info(
        "btc5m worker started (spot=%ss market=%ss)",
        spot_interval,
        market_interval,
    )

    try:
        while shutdown_q.empty():
            spot.tick()

            now = time.time()
            if now - last_market_poll >= market_interval:
                try:
                    markets = scanner.scan()
                    for m in markets:
                        repo.upsert_market(m)
                except Exception:
                    logger.exception("btc5m scan failed")

                try:
                    active = repo.get_active_markets(datetime.now(timezone.utc))
                    for row in active:
                        decider.evaluate(row)
                except Exception:
                    logger.exception("btc5m decider failed")

                try:
                    resolver.resolve_due_markets()
                except Exception:
                    logger.exception("btc5m resolver failed")

                last_market_poll = now

            time.sleep(spot_interval)
    finally:
        spot.close()
        scanner.close()
        logger.info("btc5m worker stopped")

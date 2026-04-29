"""PolymarketBookFetcher: orderbook adapter for the quant decider.

Implements the BookFetcher protocol from polyagent/services/quant/short_horizon/decider.py
on top of PolyAgent's existing Polymarket CLOB client.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from polyagent.data.clients.polymarket import PolymarketClient

logger = logging.getLogger("polyagent.services.quant.short_horizon.book")


class PolymarketBookFetcher:
    """BookFetcher backed by PolyAgent's existing Polymarket CLOB client."""

    def __init__(self, client: PolymarketClient) -> None:
        self._client = client

    def fetch_mid(self, token_id: str) -> tuple[Decimal, Decimal] | None:
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

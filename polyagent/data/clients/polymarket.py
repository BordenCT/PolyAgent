"""Polymarket CLOB API client."""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from polyagent.models import MarketData

logger = logging.getLogger("polyagent.clients.polymarket")


class PolymarketClient:
    """Wraps the Polymarket CLOB REST API and CLI."""

    def __init__(self, base_url: str = "https://clob.polymarket.com") -> None:
        self._base_url = base_url
        self._http = httpx.Client(base_url=base_url, timeout=30.0)

    def fetch_markets(self, limit: int = 500) -> list[dict]:
        """Fetch active markets from the CLOB API with cursor-based pagination.

        Args:
            limit: Maximum number of markets to return.

        Returns:
            List of raw market dicts from the API.
        """
        markets = []
        next_cursor = None

        while len(markets) < limit:
            params = {"limit": min(100, limit - len(markets)), "active": "true"}
            if next_cursor:
                params["next_cursor"] = next_cursor

            resp = self._http.get("/markets", params=params)
            resp.raise_for_status()
            data = resp.json()

            batch = data.get("data", data) if isinstance(data, dict) else data
            if not batch:
                break
            markets.extend(batch if isinstance(batch, list) else [batch])

            next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
            if not next_cursor:
                break

        logger.info("Fetched %d markets from CLOB API", len(markets))
        return markets[:limit]

    def parse_market(self, raw: dict) -> MarketData | None:
        """Parse raw API response into a MarketData model.

        Args:
            raw: Raw market dict from the CLOB API.

        Returns:
            A MarketData instance, or None if the market has no tokens.
        """
        tokens = raw.get("tokens", [])
        if not tokens:
            return None

        yes_token = next((t for t in tokens if t.get("outcome") == "Yes"), tokens[0])

        best_bid = raw.get("best_bid", 0) or 0
        best_ask = raw.get("best_ask", 0) or 0
        midpoint = (float(best_bid) + float(best_ask)) / 2

        end_date_str = raw.get("end_date_iso", "")
        if end_date_str:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            hours_left = (end_date - datetime.now(timezone.utc)).total_seconds() / 3600
        else:
            hours_left = 999.0

        return MarketData(
            polymarket_id=raw["condition_id"],
            question=raw.get("question", ""),
            category=raw.get("category", "unknown"),
            token_id=yes_token["token_id"],
            midpoint_price=Decimal(str(round(midpoint, 4))),
            bids_depth=Decimal(str(raw.get("bid_depth", 0) or 0)),
            asks_depth=Decimal(str(raw.get("ask_depth", 0) or 0)),
            hours_to_resolution=max(0.0, hours_left),
            volume_24h=Decimal(str(raw.get("volume", 0) or 0)),
        )

    def fetch_order_book(self, token_id: str) -> dict:
        """Fetch order book for a specific token via CLI subprocess.

        Args:
            token_id: The outcome token ID to fetch the book for.

        Returns:
            Parsed order book dict, or empty dict on failure.
        """
        try:
            result = subprocess.run(
                ["polymarket", "clob", "book", token_id, "-o", "json"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
            logger.warning("CLI order book fetch failed for %s: %s", token_id, e)
        return {}

    def close(self) -> None:
        """Close the HTTP client."""
        self._http.close()

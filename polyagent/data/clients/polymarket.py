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

    def fetch_market_state(self, condition_id: str) -> dict | None:
        """Fetch a fresh price + 24h volume snapshot for one market.

        Used by the exit monitor to refresh current_price and detect volume spikes.

        Args:
            condition_id: Polymarket market condition id.

        Returns:
            Dict with keys 'midpoint_price' and 'volume_24h', or None on failure.
        """
        try:
            resp = self._http.get(f"/markets/{condition_id}")
            resp.raise_for_status()
            raw = resp.json()
            best_bid = float(raw.get("best_bid", 0) or 0)
            best_ask = float(raw.get("best_ask", 0) or 0)
            midpoint = (best_bid + best_ask) / 2 if best_bid and best_ask else best_bid or best_ask
            return {
                "midpoint_price": Decimal(str(round(midpoint, 4))),
                "volume_24h": Decimal(str(raw.get("volume", 0) or 0)),
            }
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("Failed to refresh market state for %s: %s", condition_id, e)
            return None

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> dict:
        """Place an order on the CLOB via the polymarket CLI.

        Returns a dict with:
            - 'ok': bool — True on exit code 0 with parsable JSON
            - 'request': echo of the invocation args
            - 'response': parsed JSON response on success
            - 'error': error message on failure
            - 'stderr': captured stderr for diagnostics

        Args:
            token_id: The outcome token ID to trade.
            side: "BUY" or "SELL".
            price: Limit price in the 0-1 range.
            size: Notional size in USD.
        """
        request = {
            "token_id": token_id,
            "side": side.upper(),
            "price": price,
            "size": size,
        }

        cmd = [
            "polymarket", "clob", "order", "create",
            "--token-id", token_id,
            "--side", side.upper(),
            "--price", str(price),
            "--size", str(size),
            "-o", "json",
        ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return {"ok": False, "request": request, "error": f"subprocess failed: {e}"}

        if result.returncode != 0:
            return {
                "ok": False,
                "request": request,
                "error": f"exit code {result.returncode}",
                "stderr": result.stderr,
            }

        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            return {
                "ok": False,
                "request": request,
                "error": f"invalid JSON response: {e}",
                "stderr": result.stderr,
            }

        return {"ok": True, "request": request, "response": response}

    def close(self) -> None:
        """Close the HTTP client."""
        self._http.close()

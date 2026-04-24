"""Polymarket CLOB API client."""
from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from polyagent.models import MarketData

logger = logging.getLogger("polyagent.clients.polymarket")

_GAMMA_BASE = "https://gamma-api.polymarket.com"


class PolymarketClient:
    """Wraps the Polymarket CLOB REST API and CLI."""

    def __init__(self, base_url: str = "https://clob.polymarket.com") -> None:
        self._base_url = base_url
        self._http = httpx.Client(base_url=base_url, timeout=30.0)
        self._gamma = httpx.Client(base_url=_GAMMA_BASE, timeout=30.0)

    def fetch_markets(self, limit: int = 500) -> list[dict]:
        """Fetch active, non-closed markets from the Gamma API sorted by 24h volume.

        The CLOB /markets endpoint paginates from oldest to newest and returns
        closed markets with no price or depth data. Gamma returns only live
        markets and includes liquidityNum and volume24hr.

        Args:
            limit: Maximum number of markets to return.

        Returns:
            List of raw Gamma market dicts.
        """
        markets: list[dict] = []
        offset = 0
        per_page = min(100, limit)

        while len(markets) < limit:
            params = {
                "active": "true",
                "closed": "false",
                "limit": min(per_page, limit - len(markets)),
                "offset": offset,
                "order": "volume24hr",
                "ascending": "false",
            }
            resp = self._gamma.get("/markets", params=params)
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            markets.extend(batch)
            offset += len(batch)
            if len(batch) < per_page:
                break

        logger.info("Fetched %d markets from Gamma API", len(markets))
        return markets[:limit]

    def parse_market(self, raw: dict) -> MarketData | None:
        """Parse a Gamma API market dict into a MarketData model.

        Args:
            raw: Raw market dict from the Gamma API.

        Returns:
            A MarketData instance, or None if required fields are missing.
        """
        condition_id = raw.get("conditionId")
        if not condition_id:
            return None

        try:
            token_ids: list[str] = json.loads(raw.get("clobTokenIds") or "[]")
        except (json.JSONDecodeError, TypeError):
            return None
        if not token_ids:
            return None

        try:
            prices: list[str] = json.loads(raw.get("outcomePrices") or "[]")
            yes_price = float(prices[0]) if prices else 0.5
        except (json.JSONDecodeError, TypeError, IndexError, ValueError):
            yes_price = 0.5

        end_date_str = raw.get("endDate", "")
        if end_date_str:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            hours_left = (end_date - datetime.now(timezone.utc)).total_seconds() / 3600
        else:
            hours_left = 999.0

        volume_24h = Decimal(str(raw.get("volume24hr") or 0))

        return MarketData(
            polymarket_id=condition_id,
            question=raw.get("question", ""),
            category=raw.get("category", "unknown"),
            token_id=token_ids[0],
            midpoint_price=Decimal(str(round(yes_price, 4))),
            bids_depth=volume_24h,
            asks_depth=volume_24h,
            hours_to_resolution=max(0.0, hours_left),
            volume_24h=volume_24h,
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
        """Fetch a fresh price + volume + resolution snapshot for one market.

        Used by the exit monitor to refresh current_price and decide whether a
        zero price means "market resolved NO" vs "book is temporarily empty".
        Retries once after a back-off on 429 (rate limit).

        Resolution is detected via ``tokens[i].winner``. The CLOB sets this
        only on final resolution, which avoids treating paused or delisted
        markets (``closed=true`` without a winner) as resolved. For resolved
        markets ``midpoint_price`` is pinned to 1.0 or 0.0 in YES coordinates
        based on which token won — not derived from the (empty) order book —
        so downstream P&L sees the real payoff.

        Args:
            condition_id: Polymarket market condition id.

        Returns:
            Dict with keys 'midpoint_price', 'volume_24h', 'is_resolved',
            or None on failure.
        """
        for attempt in range(2):
            try:
                resp = self._http.get(f"/markets/{condition_id}")
                resp.raise_for_status()
                raw = resp.json()

                tokens = raw.get("tokens") or []
                yes_token = next(
                    (t for t in tokens if str(t.get("outcome", "")).strip().lower() == "yes"),
                    None,
                )
                winner_token = next(
                    (t for t in tokens if t.get("winner") is True),
                    None,
                )
                is_resolved = winner_token is not None

                if is_resolved:
                    winner_id = winner_token.get("token_id")
                    yes_id = yes_token.get("token_id") if yes_token else None
                    if yes_token is None:
                        # Categorical / non-binary market — PolyAgent's scanner
                        # should not enter these, but fail loud if one slips in.
                        logger.warning(
                            "Resolved market %s has no 'Yes' token; tokens=%s",
                            condition_id,
                            [t.get("outcome") for t in tokens],
                        )
                        current_price = Decimal("0")
                    elif winner_id and yes_id and winner_id == yes_id:
                        current_price = Decimal("1")
                    else:
                        current_price = Decimal("0")
                else:
                    best_bid = float(raw.get("best_bid", 0) or 0)
                    best_ask = float(raw.get("best_ask", 0) or 0)
                    midpoint = (
                        (best_bid + best_ask) / 2
                        if best_bid and best_ask
                        else best_bid or best_ask
                    )
                    current_price = Decimal(str(round(midpoint, 4)))

                return {
                    "midpoint_price": current_price,
                    "volume_24h": Decimal(str(raw.get("volume", 0) or 0)),
                    "is_resolved": is_resolved,
                }
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt == 0:
                    retry_after = float(e.response.headers.get("Retry-After", "5"))
                    logger.info(
                        "Rate limited on %s — backing off %.1fs",
                        condition_id, retry_after,
                    )
                    time.sleep(retry_after)
                    continue
                logger.warning("Failed to refresh market state for %s: %s", condition_id, e)
                return None
            except (httpx.HTTPError, ValueError) as e:
                logger.warning("Failed to refresh market state for %s: %s", condition_id, e)
                return None
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
        """Close the HTTP clients."""
        self._http.close()
        self._gamma.close()

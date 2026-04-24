"""Coinbase BTC/USD spot source with rolling realized-vol.

Public interface (PriceSource protocol):
    tick()          -> Decimal | None
    current()       -> Decimal | None
    realized_vol(s) -> float (annualised σ)

Designed so a ChainlinkSpotSource subclass can slot in with no other
changes once Data Streams credentials arrive.
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from decimal import Decimal

import httpx

logger = logging.getLogger("polyagent.services.btc5m.spot")

_COINBASE_TICKER_URL = (
    "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
)
_SECONDS_PER_YEAR = 365.25 * 24 * 3600


class BtcSpotSource:
    """In-memory rolling cache of BTC/USD mid prices from Coinbase."""

    def __init__(self, _max_age_s: int = 3600, timeout_s: float = 5.0) -> None:
        self._max_age_s = _max_age_s
        self._buf: deque[tuple[float, Decimal]] = deque()
        self._http = httpx.Client(timeout=timeout_s)

    def _fetch_ticker(self) -> Decimal:
        """Fetch the current BTC/USD price from Coinbase, as the mid of bid/ask.

        Raises on HTTP error.
        """
        resp = self._http.get(_COINBASE_TICKER_URL)
        resp.raise_for_status()
        body = resp.json()
        bid = Decimal(str(body["bid"]))
        ask = Decimal(str(body["ask"]))
        return (bid + ask) / Decimal("2")

    def tick(self) -> Decimal | None:
        """Fetch a spot price, append to buffer, return it.

        Returns None on HTTP/parse error (logged but not raised).
        """
        try:
            price = self._fetch_ticker()
        except Exception as exc:
            logger.warning("spot tick failed: %s", exc)
            return None

        now = time.time()
        self._buf.append((now, price))

        cutoff = now - self._max_age_s
        while self._buf and self._buf[0][0] < cutoff:
            self._buf.popleft()

        return price

    def current(self) -> Decimal | None:
        """Return the most recent cached price, or None if we haven't ticked."""
        if not self._buf:
            return None
        return self._buf[-1][1]

    def realized_vol(self, window_s: int = 300) -> float:
        """Annualised σ of log returns over the trailing window.

        Returns 0.0 if fewer than 2 samples fall in the window.
        """
        if not self._buf:
            return 0.0
        # Anchor to the most recent buffered timestamp rather than the live
        # clock so that callers who mock time.time() during ticks but not
        # during this call still get correct windowing.
        latest_ts = self._buf[-1][0]
        cutoff = latest_ts - window_s
        samples = [(t, p) for (t, p) in self._buf if t >= cutoff]
        if len(samples) < 2:
            return 0.0

        log_returns: list[float] = []
        for i in range(1, len(samples)):
            prev_p = float(samples[i - 1][1])
            curr_p = float(samples[i][1])
            if prev_p <= 0 or curr_p <= 0:
                continue
            log_returns.append(math.log(curr_p / prev_p))
        if len(log_returns) < 2:
            return 0.0

        mean = sum(log_returns) / len(log_returns)
        variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
        # Per-return variance. Scale to per-second, then annualise.
        # Samples are not exactly evenly spaced; use window length as proxy.
        span_s = samples[-1][0] - samples[0][0]
        if span_s <= 0:
            return 0.0
        per_s_variance = variance * len(log_returns) / span_s
        return math.sqrt(per_s_variance * _SECONDS_PER_YEAR)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

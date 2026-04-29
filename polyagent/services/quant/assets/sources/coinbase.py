"""Coinbase price + settlement source.

Implements both PriceSource (tick / current / realized_vol) and
SettlementSource (price_at / source_id) against the public Coinbase
Exchange API.
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from datetime import datetime
from decimal import Decimal

import httpx

logger = logging.getLogger("polyagent.services.quant.assets.sources.coinbase")

_TICKER_URL_FMT = "https://api.exchange.coinbase.com/products/{product}/ticker"
_CANDLES_URL_FMT = "https://api.exchange.coinbase.com/products/{product}/candles"
_SECONDS_PER_YEAR = 365.25 * 24 * 3600


class CoinbaseSpotSource:
    """In-memory rolling buffer of Coinbase mid prices, plus historical lookup.

    Implements both the PriceSource and SettlementSource Protocols defined in
    polyagent.services.quant.assets.sources.base.

    The ``_max_age_s`` kwarg keeps the leading underscore for backward
    compatibility with the original btc5m subsystem. Old call-sites pass it
    by name.
    """

    def __init__(
        self,
        product: str = "BTC-USD",
        _max_age_s: int = 3600,
        timeout_s: float = 5.0,
        http_client=None,
    ) -> None:
        self._product = product
        self._ticker_url = _TICKER_URL_FMT.format(product=product)
        self._candles_url = _CANDLES_URL_FMT.format(product=product)
        self._max_age_s = _max_age_s
        self._buf: deque[tuple[float, Decimal]] = deque()
        self._http = http_client or httpx.Client(timeout=timeout_s)

    @property
    def product(self) -> str:
        return self._product

    def source_id(self) -> str:
        """Return ``coinbase:<product>`` (SettlementSource Protocol)."""
        return f"coinbase:{self._product}"

    def _fetch_ticker(self) -> Decimal:
        """Fetch the current mid price from Coinbase. Raises on HTTP error."""
        resp = self._http.get(self._ticker_url)
        resp.raise_for_status()
        body = resp.json()
        bid = Decimal(str(body["bid"]))
        ask = Decimal(str(body["ask"]))
        return (bid + ask) / Decimal("2")

    def tick(self) -> Decimal | None:
        """Fetch a spot price, append to the rolling buffer, return it.

        Returns None on HTTP/parse error (logged but not raised).
        """
        try:
            price = self._fetch_ticker()
        except Exception as exc:
            logger.warning("%s tick failed: %s", self._product, exc)
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
        """Annualised sigma of log returns over the trailing window.

        Returns 0.0 if fewer than 2 samples fall in the window.
        """
        if not self._buf:
            return 0.0
        # Anchor to the most recent buffered timestamp rather than the live
        # clock so callers that mock time.time() during ticks but not during
        # this call still get correct windowing.
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
        span_s = samples[-1][0] - samples[0][0]
        if span_s <= 0:
            return 0.0
        per_s_variance = variance * len(log_returns) / span_s
        return math.sqrt(per_s_variance * _SECONDS_PER_YEAR)

    def price_at(self, ts: datetime) -> Decimal | None:
        """Return the historical close nearest to ``ts`` (SettlementSource).

        Pulls a 60-second-granularity candle window centered on ``ts`` from
        Coinbase and returns the close of the candle whose timestamp is
        closest to the target. Returns None on HTTP error or empty response.
        """
        target = int(ts.timestamp())
        try:
            resp = self._http.get(
                self._candles_url,
                params={"granularity": 60, "start": target - 60, "end": target + 60},
            )
            resp.raise_for_status()
            candles = resp.json()
        except Exception as exc:
            logger.warning(
                "%s candle fetch failed for ts=%s: %s", self._product, ts, exc
            )
            return None
        if not candles:
            return None
        # Coinbase candle layout: [time, low, high, open, close, volume]
        best = min(candles, key=lambda c: abs(c[0] - target))
        return Decimal(str(best[4]))

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

"""Chainlink Data Feed price + settlement source.

Reads the on-chain Chainlink AggregatorV3 BTC/USD (or ETH/USD, etc.)
price feed on Polygon mainnet via JSON-RPC ``eth_call``. Polymarket's
crypto up/down markets resolve against the Chainlink data stream, so
quoting and resolution against the same oracle removes the basis drift
that a Coinbase or Binance source would introduce.

Encodes/decodes ABI manually instead of pulling in ``web3.py`` so the
runtime cost is one ``httpx`` dep we already have. Two contract
methods are used:

- ``latestRoundData()`` selector ``0xfeaf968c`` for current price.
- ``getRoundData(uint80)`` selector ``0x9a6fc8f5`` for historical
  rounds, walked backward from the latest round when serving
  :meth:`price_at` for a past timestamp.

Both return ``(uint80 roundId, int256 answer, uint256 startedAt,
uint256 updatedAt, uint80 answeredInRound)``, encoded as 5 right-aligned
32-byte slots. ``answer`` is the price scaled by ``decimals()``
(typically 8 for crypto-USD pairs on Polygon).
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from datetime import datetime
from decimal import Decimal

import httpx

logger = logging.getLogger("polyagent.services.quant.assets.sources.chainlink")

# publicnode.com offers an open Polygon endpoint with no key required.
# polygon-rpc.com (the previous default) started 401-ing unauthenticated
# requests in mid-2026. Production should set POLYGON_RPC_URL to a
# private RPC (Alchemy, QuickNode, dRPC, Ankr...) for reliability and
# higher rate limits; this default is a best-effort fallback.
_DEFAULT_RPC_URL = "https://polygon-bor-rpc.publicnode.com"
_SECONDS_PER_YEAR = 365.25 * 24 * 3600

# Polygon mainnet AggregatorV3 addresses for common crypto-USD pairs.
# https://docs.chain.link/data-feeds/price-feeds/addresses?network=polygon
POLYGON_AGGREGATORS: dict[str, str] = {
    "BTC-USD": "0xc907E116054Ad103354f2D350FD2514433D57F6f",
    "ETH-USD": "0xF9680D99D6C9589e2a93a78A04A279e509205945",
}

_LATEST_ROUND_DATA_SELECTOR = "0xfeaf968c"
_GET_ROUND_DATA_SELECTOR = "0x9a6fc8f5"
_ROUND_DATA_BYTES = 5 * 32
# A safety cap on how far to walk back from the latest round. Polygon
# BTC/USD ticks roughly every 60s on heartbeat, so 240 rounds covers
# ~4 hours of history. Any price_at lookup older than that is best-served
# by an out-of-band historical archive, which we don't keep here.
_MAX_ROUND_WALK = 240


def _encode_round_call(round_id: int) -> str:
    """ABI-encode ``getRoundData(uint80)`` call data for ``round_id``."""
    return _GET_ROUND_DATA_SELECTOR + round_id.to_bytes(32, "big").hex()


def _decode_round_data(hex_result: str) -> tuple[int, int, int, int, int]:
    """Decode the 5-tuple returned by ``latestRoundData`` / ``getRoundData``.

    Returns:
        ``(roundId, answer, startedAt, updatedAt, answeredInRound)``.
        ``answer`` is the raw integer value, not yet divided by 10^decimals.
        Caller scales by the aggregator's ``decimals()`` to get the price.

    Raises:
        ValueError: on malformed return data.
    """
    if not hex_result or not hex_result.startswith("0x"):
        raise ValueError(f"bad eth_call result: {hex_result!r}")
    payload = bytes.fromhex(hex_result[2:])
    if len(payload) < _ROUND_DATA_BYTES:
        raise ValueError(
            f"round data too short: {len(payload)} bytes, expected >= {_ROUND_DATA_BYTES}"
        )
    round_id = int.from_bytes(payload[0:32], "big")
    answer = int.from_bytes(payload[32:64], "big", signed=True)
    started_at = int.from_bytes(payload[64:96], "big")
    updated_at = int.from_bytes(payload[96:128], "big")
    answered_in_round = int.from_bytes(payload[128:160], "big")
    return round_id, answer, started_at, updated_at, answered_in_round


class ChainlinkDataFeedSource:
    """Polygon AggregatorV3 reader for one crypto-USD pair.

    Implements both PriceSource (``tick`` / ``current`` / ``realized_vol``)
    and SettlementSource (``price_at`` / ``source_id``).

    Args:
        pair: Symbol like ``BTC-USD`` or ``ETH-USD``. Used for
            ``source_id`` and to resolve the default aggregator address
            from :data:`POLYGON_AGGREGATORS`.
        aggregator_address: Override for the on-chain aggregator. When
            None, falls back to :data:`POLYGON_AGGREGATORS[pair]`.
        rpc_url: Polygon JSON-RPC endpoint.
        decimals: Number of decimals the aggregator reports. Polygon
            crypto-USD feeds are 8.
        max_age_s: Rolling tick buffer retention for vol calibration.
        timeout_s: HTTP timeout for RPC calls.
        http_client: Inject a pre-built client (used by tests).
    """

    def __init__(
        self,
        pair: str = "BTC-USD",
        aggregator_address: str | None = None,
        rpc_url: str = _DEFAULT_RPC_URL,
        decimals: int = 8,
        max_age_s: int = 3600,
        timeout_s: float = 10.0,
        http_client=None,
    ) -> None:
        self._pair = pair
        self._addr = aggregator_address or POLYGON_AGGREGATORS.get(pair)
        if not self._addr:
            raise ValueError(f"no Polygon aggregator for pair {pair!r}")
        self._rpc_url = rpc_url
        self._decimals = decimals
        self._scale = Decimal(10) ** decimals
        self._max_age_s = max_age_s
        self._buf: deque[tuple[float, Decimal]] = deque()
        self._http = http_client or httpx.Client(timeout=timeout_s)
        self._req_id = 0

    @property
    def pair(self) -> str:
        return self._pair

    def source_id(self) -> str:
        """Return ``chainlink:polygon:<pair>`` (SettlementSource Protocol)."""
        return f"chainlink:polygon:{self._pair}"

    def _eth_call(self, data: str) -> str:
        """One JSON-RPC ``eth_call`` against the aggregator. Returns hex result."""
        self._req_id += 1
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": self._addr, "data": data}, "latest"],
            "id": self._req_id,
        }
        resp = self._http.post(self._rpc_url, json=payload)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"RPC error: {body['error']}")
        return body["result"]

    def _scale_answer(self, answer: int) -> Decimal:
        return Decimal(answer) / self._scale

    def _latest_round(self) -> tuple[int, Decimal, int]:
        """Return ``(roundId, price, updatedAt)`` for the latest round."""
        result = self._eth_call(_LATEST_ROUND_DATA_SELECTOR)
        round_id, answer, _, updated_at, _ = _decode_round_data(result)
        return round_id, self._scale_answer(answer), updated_at

    def _get_round(self, round_id: int) -> tuple[Decimal, int]:
        """Return ``(price, updatedAt)`` for a specific round."""
        result = self._eth_call(_encode_round_call(round_id))
        _, answer, _, updated_at, _ = _decode_round_data(result)
        return self._scale_answer(answer), updated_at

    def tick(self) -> Decimal | None:
        """Fetch the current price and append to the rolling buffer.

        Returns None on RPC/parse error (logged but not raised).
        """
        try:
            _, price, _ = self._latest_round()
        except Exception as exc:
            logger.warning("%s tick failed: %s", self._pair, exc)
            return None
        now = time.time()
        self._buf.append((now, price))
        cutoff = now - self._max_age_s
        while self._buf and self._buf[0][0] < cutoff:
            self._buf.popleft()
        return price

    def current(self) -> Decimal | None:
        """Most recent buffered price, or None if we haven't ticked yet."""
        if not self._buf:
            return None
        return self._buf[-1][1]

    def realized_vol(self, window_s: int = 300) -> float:
        """Annualised sigma of log returns over the trailing window.

        Returns 0.0 if fewer than two samples fall in the window. Mirrors
        the Coinbase source's calculation so the same vol calibration
        rules apply.
        """
        if not self._buf:
            return 0.0
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
        """Return the Chainlink price effective at ``ts``.

        Walks back from the latest round until finding the first round
        whose ``updatedAt <= ts`` (the price that was canonical on-chain
        at the queried instant). Bounded by :data:`_MAX_ROUND_WALK` to
        avoid runaway RPC usage on stale lookups.

        Returns None on RPC error, malformed payload, or when the walk
        exhausts the cap without finding an old-enough round.
        """
        target = int(ts.timestamp())
        try:
            latest_round_id, latest_price, latest_updated = self._latest_round()
        except Exception as exc:
            logger.warning("%s latestRoundData failed: %s", self._pair, exc)
            return None

        if latest_updated <= target:
            return latest_price

        round_id = latest_round_id
        for _ in range(_MAX_ROUND_WALK):
            round_id -= 1
            if round_id <= 0:
                return None
            try:
                price, updated_at = self._get_round(round_id)
            except Exception as exc:
                logger.warning(
                    "%s getRoundData(%d) failed: %s",
                    self._pair, round_id, exc,
                )
                return None
            if updated_at <= target:
                return price
        logger.warning(
            "%s price_at(%s): walked %d rounds without finding pre-target round",
            self._pair, ts, _MAX_ROUND_WALK,
        )
        return None

    def close(self) -> None:
        """Release the underlying HTTP client."""
        self._http.close()

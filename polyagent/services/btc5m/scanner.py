"""Scans Polymarket Gamma for new BTC short-horizon up/down markets.

Accepts any ``btc-updown-<duration>-<unix_ts>`` slug where ``<duration>``
is an integer followed by m/h/d (e.g. 5m, 15m, 1h, 4h, 1d). The specific
duration is preserved on the model as ``window_duration_s``.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

from polyagent.models import Btc5mMarket

logger = logging.getLogger("polyagent.services.btc5m.scanner")

# Captures (duration_token, unix_ts). duration_token examples: 5m, 15m, 1h, 4h, 1d.
BTC5M_SLUG_RE = re.compile(r"^btc-updown-(\d+[mhd])-(\d+)$")

_GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

_UNIT_TO_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def _duration_to_seconds(token: str) -> int:
    """Convert a duration token to seconds.

    Args:
        token: A string like '5m', '15m', '1h', '4h', '1d'.

    Returns:
        Duration in seconds.

    Raises:
        ValueError: If the token has an unrecognized unit or non-positive integer.
    """
    if not token or token[-1] not in _UNIT_TO_SECONDS:
        raise ValueError(f"bad duration token: {token!r}")
    try:
        n = int(token[:-1])
    except ValueError as exc:
        raise ValueError(f"bad duration number: {token!r}") from exc
    if n <= 0:
        raise ValueError(f"non-positive duration: {token!r}")
    return n * _UNIT_TO_SECONDS[token[-1]]


def parse_btc5m_slug(slug: str) -> tuple[datetime, datetime, int]:
    """Extract (window_start_ts, window_end_ts, window_duration_s) from a slug.

    Args:
        slug: A market slug of the form ``btc-updown-<duration>-<unix_ts>``.

    Returns:
        A 3-tuple of (window_start, window_end, duration_s) where both
        timestamps are timezone-aware UTC datetimes and duration_s is seconds.

    Raises:
        ValueError: For non-BTC slugs, bad duration tokens, or malformed timestamps.
    """
    m = BTC5M_SLUG_RE.match(slug)
    if not m:
        raise ValueError(f"not a btc-updown slug: {slug!r}")
    duration_token = m.group(1)
    try:
        end_unix = int(m.group(2))
    except ValueError as exc:
        raise ValueError(f"malformed timestamp in slug: {slug!r}") from exc
    duration_s = _duration_to_seconds(duration_token)
    window_end = datetime.fromtimestamp(end_unix, tz=timezone.utc)
    window_start = window_end - timedelta(seconds=duration_s)
    return window_start, window_end, duration_s


class Btc5mScanner:
    """Polls Gamma for BTC short-horizon markets and returns parsed objects.

    Args:
        http_client: An httpx.Client (or mock with the same interface). A default
                     client with a 15-second timeout is created if not provided.
        page_limit: Max number of markets to request per call to Gamma.
    """

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        page_limit: int = 500,
    ) -> None:
        self._http = http_client or httpx.Client(timeout=15.0)
        self._page_limit = page_limit

    def scan(self) -> list[Btc5mMarket]:
        """Return all currently-listed BTC up/down markets. Empty list on error.

        Returns:
            Parsed Btc5mMarket objects for every active btc-updown-* slug found
            in Gamma. Returns an empty list if the HTTP call fails or returns
            a non-200 status.
        """
        try:
            resp = self._http.get(
                _GAMMA_MARKETS_URL,
                params={"active": "true", "closed": "false", "limit": self._page_limit},
            )
            if resp.status_code != 200:
                logger.warning("gamma returned %s", resp.status_code)
                return []
            raw = resp.json()
        except Exception as exc:
            logger.warning("gamma fetch failed: %s", exc)
            return []

        out: list[Btc5mMarket] = []
        for m in raw:
            slug = m.get("slug") or ""
            if not BTC5M_SLUG_RE.match(slug):
                continue
            try:
                window_start, window_end, duration_s = parse_btc5m_slug(slug)
                token_ids = json.loads(m.get("clobTokenIds") or "[]")
                if len(token_ids) < 2:
                    continue
                out.append(Btc5mMarket(
                    polymarket_id=m.get("conditionId") or "",
                    slug=slug,
                    token_id_yes=token_ids[0],
                    token_id_no=token_ids[1],
                    window_duration_s=duration_s,
                    window_start_ts=window_start,
                    window_end_ts=window_end,
                ))
            except Exception as exc:
                logger.warning("parse failed for %s: %s", slug, exc)
                continue

        return out

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

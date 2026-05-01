"""Registry-aware short-horizon slug scanner.

Matches ``^(token1|token2|...)-updown-(\\d+[mhd])-(\\d+)$`` where the token
union is built from ``registry.enabled_for(MarketFamily.SHORT_HORIZON)``.
This keeps the scanner asset-agnostic: adding a new asset to the registry
with ``MarketFamily.SHORT_HORIZON`` automatically widens what gets picked up.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

from polyagent.models import QuantShortMarket
from polyagent.services.quant.assets.registry import enabled_for
from polyagent.services.quant.assets.spec import MarketFamily

logger = logging.getLogger("polyagent.services.quant.short_horizon.scanner")

_GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
_UNIT_TO_SECONDS = {"m": 60, "h": 3600, "d": 86400}

# Token-label pairings for binary markets. "Up" maps to the YES axis on
# our side ("price went up" == "YES wins"), "Down" maps to NO.
# Same set lives in polyagent.data.clients.polymarket; both must agree.
_YES_OUTCOME_LABELS = frozenset({"yes", "up"})
_NO_OUTCOME_LABELS = frozenset({"no", "down"})


def _build_slug_regex() -> re.Pattern[str]:
    """Return a compiled regex matching any registered short-horizon slug.

    The token alternation is rebuilt on every call so registry changes via
    test fixtures are picked up without process restart.
    """
    tokens = [s.slug_token for s in enabled_for(MarketFamily.SHORT_HORIZON) if s.slug_token]
    if not tokens:
        # No assets enabled, match nothing (always-fail pattern).
        return re.compile(r"(?!)")
    union = "|".join(re.escape(t) for t in tokens)
    return re.compile(rf"^({union})-updown-(\d+[mhd])-(\d+)$")


def _duration_to_seconds(token: str) -> int:
    """Translate a duration token like ``5m`` or ``1h`` into seconds."""
    if not token or token[-1] not in _UNIT_TO_SECONDS:
        raise ValueError(f"bad duration token: {token!r}")
    n = int(token[:-1])
    if n <= 0:
        raise ValueError(f"non-positive duration: {token!r}")
    return n * _UNIT_TO_SECONDS[token[-1]]


def _pair_outcome_tokens(
    slug: str,
    outcomes: list,
    token_ids: list,
) -> tuple[str, str] | None:
    """Pair (token_id_yes, token_id_no) by reading outcome labels.

    Earlier the scanner trusted ``token_ids[0]`` to be the YES side and
    ``token_ids[1]`` the NO side by position. Polymarket's Gamma API
    pairs ``outcomes`` to ``clobTokenIds`` by index but the binding is
    not guaranteed to be in any particular order, so a ``[Down, Up]``
    response would silently flip every trade onto the wrong token.

    This pair-by-label helper makes the binding explicit. Returns None
    (and logs) when the input is malformed or the labels can't be
    interpreted, so the scanner can skip the market instead of writing
    a corrupted row.
    """
    if not isinstance(outcomes, list) or not isinstance(token_ids, list):
        logger.warning("non-list outcomes/tokens for %s", slug)
        return None
    if len(outcomes) != 2 or len(token_ids) != 2:
        logger.warning(
            "expected 2 outcomes and 2 tokens for %s, got outcomes=%d tokens=%d",
            slug, len(outcomes), len(token_ids),
        )
        return None
    yes_idx = next(
        (i for i, o in enumerate(outcomes)
         if str(o).strip().lower() in _YES_OUTCOME_LABELS),
        None,
    )
    no_idx = next(
        (i for i, o in enumerate(outcomes)
         if str(o).strip().lower() in _NO_OUTCOME_LABELS),
        None,
    )
    if yes_idx is None or no_idx is None or yes_idx == no_idx:
        logger.warning(
            "could not pair outcome labels for %s: outcomes=%s",
            slug, outcomes,
        )
        return None
    return token_ids[yes_idx], token_ids[no_idx]


def parse_short_horizon_slug(slug: str) -> tuple[str, datetime, datetime, int]:
    """Decode a Polymarket short-horizon slug.

    Args:
        slug: Slug of the form ``<token>-updown-<duration><unit>-<unix_ts>``.

    Returns:
        ``(asset_id, window_start, window_end, duration_s)`` where the
        timestamps are timezone-aware UTC datetimes.

    Raises:
        ValueError: If the slug does not match any registered short-horizon
            asset, or the duration token is malformed.
    """
    pattern = _build_slug_regex()
    m = pattern.match(slug)
    if not m:
        raise ValueError(
            f"slug does not match any registered short-horizon asset: {slug!r}"
        )
    token, duration_token, end_unix = m.group(1), m.group(2), int(m.group(3))
    asset_id = next(
        (s.asset_id for s in enabled_for(MarketFamily.SHORT_HORIZON) if s.slug_token == token),
        None,
    )
    if asset_id is None:
        raise ValueError(f"no asset with slug_token={token!r}")
    duration_s = _duration_to_seconds(duration_token)
    window_end = datetime.fromtimestamp(end_unix, tz=timezone.utc)
    window_start = window_end - timedelta(seconds=duration_s)
    return asset_id, window_start, window_end, duration_s


class QuantShortScanner:
    """Polls Polymarket Gamma for any registered short-horizon market.

    Args:
        http_client: An httpx-style client with a ``get(url, params=...)``
            method. Defaults to a fresh ``httpx.Client`` with a 15s timeout.
        page_limit: Number of markets requested per Gamma page.
    """

    def __init__(self, http_client=None, page_limit: int = 1000) -> None:
        self._http = http_client or httpx.Client(timeout=15.0)
        self._page_limit = page_limit

    def scan(self) -> list[QuantShortMarket]:
        """Return one QuantShortMarket per Gamma row whose slug matches.

        Network and parse errors are logged and swallowed so the orchestrator
        loop never crashes on a flaky upstream.
        """
        try:
            resp = self._http.get(
                _GAMMA_MARKETS_URL,
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": self._page_limit,
                    # `end_date_min=<now>` filters out zombie markets:
                    # Polymarket has thousands of months-old short-horizon
                    # markets stuck at active=true, closed=false. Without
                    # this filter the page is dominated by them and our
                    # local "window already closed" filter rejects every
                    # row, leaving the decider with zero candidates.
                    "end_date_min": datetime.now(timezone.utc).isoformat(),
                    # Soonest-resolving first puts currently-active 5m and
                    # 15m markets at the top of the page.
                    "order": "endDate",
                    "ascending": "true",
                },
            )
            if resp.status_code != 200:
                logger.warning("gamma returned %s", resp.status_code)
                return []
            raw = resp.json()
        except Exception as exc:
            logger.warning("gamma fetch failed: %s", exc)
            return []
        out: list[QuantShortMarket] = []
        pattern = _build_slug_regex()
        now = datetime.now(timezone.utc)
        # Polymarket lists 5m/15m markets up to 24+ hours in advance.
        # Without a future-cutoff the page (sorted newest-startDate first
        # to catch the rapidly-rotating short-horizon batch) is dominated
        # by tomorrow's markets and currently-live ones are pushed off the
        # 500-row page entirely. Lookahead is sized to the orchestrator's
        # market-poll cadence so we discover markets just before they open.
        lookahead = timedelta(seconds=60)
        future_cutoff = now + lookahead
        for m in raw:
            slug = m.get("slug") or ""
            if not pattern.match(slug):
                continue
            try:
                asset_id, ws, we, dur = parse_short_horizon_slug(slug)
                if ws > future_cutoff:
                    continue  # window not opening soon
                if we <= now:
                    continue  # window already closed; can't trade
                token_ids = json.loads(m.get("clobTokenIds") or "[]")
                outcomes = json.loads(m.get("outcomes") or "[]")
                paired = _pair_outcome_tokens(slug, outcomes, token_ids)
                if paired is None:
                    continue
                token_id_yes, token_id_no = paired
                out.append(QuantShortMarket(
                    polymarket_id=m.get("conditionId") or "",
                    slug=slug,
                    token_id_yes=token_id_yes,
                    token_id_no=token_id_no,
                    window_duration_s=dur,
                    window_start_ts=ws,
                    window_end_ts=we,
                    asset_id=asset_id,
                ))
            except Exception as exc:
                logger.warning("parse failed for %s: %s", slug, exc)
                continue
        return out

    def close(self) -> None:
        """Release the underlying HTTP client."""
        self._http.close()

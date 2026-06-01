"""Recover historical shadow labels for the microstructure estimator.

The bot evaluated ~5500 short-horizon markets it never traded (low edge,
or bankroll exhausted). Each evaluation logged a decision point with the
estimator output to ``quant_decider_rejections``, but the market itself
was never persisted or resolved.

This module reconstructs the labels:
  1. Pull distinct decision points (latest per market) where ``p_up`` was
     computed and the slug is parseable.
  2. Parse window timing from the slug.
  3. Fetch the authoritative outcome from Polymarket by conditionId.
  4. Upsert into ``quant_shadow_labels``.

Outcome ALWAYS comes from Polymarket's settled winner, never from spot
reconstruction (the resolver records ~43% spot/PM disagreement on these
near-ATM windows).

Runs on the host that can reach both the DB and the Polymarket API.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

import psycopg
from psycopg.rows import dict_row

from polyagent.data.clients.polymarket import PolymarketClient
from polyagent.services.quant.short_horizon.scanner import parse_short_horizon_slug

logger = logging.getLogger("polyagent.quant.ml.recover")


_SHADOW_CANDIDATES_SQL = """
    SELECT DISTINCT ON (r.polymarket_id)
        r.polymarket_id,
        r.slug,
        r.decision_ts,
        r.p_up        AS estimator_p_up,
        r.spot        AS spot_at_decision,
        r.mid,
        r.vol         AS vol_at_decision,
        r.abs_edge
    FROM quant_decider_rejections r
    WHERE r.p_up IS NOT NULL
      AND r.polymarket_id IS NOT NULL
      AND r.slug ~ '-updown-[0-9]+[mhd]-[0-9]+$'
    ORDER BY r.polymarket_id, r.decision_ts DESC
"""

_UPSERT_SQL = """
    INSERT INTO quant_shadow_labels (
        polymarket_id, slug, asset_id, decision_ts,
        estimator_p_up, spot_at_decision, mid, vol_at_decision, abs_edge,
        window_start_ts, window_end_ts, window_duration_s,
        outcome, source
    ) VALUES (
        %(polymarket_id)s, %(slug)s, %(asset_id)s, %(decision_ts)s,
        %(estimator_p_up)s, %(spot_at_decision)s, %(mid)s, %(vol_at_decision)s, %(abs_edge)s,
        %(window_start_ts)s, %(window_end_ts)s, %(window_duration_s)s,
        %(outcome)s, 'recovery'
    )
    ON CONFLICT (polymarket_id) DO UPDATE SET
        outcome = COALESCE(EXCLUDED.outcome, quant_shadow_labels.outcome),
        recovered_at = NOW()
"""


@dataclass
class RecoveryStats:
    candidates: int = 0
    parsed: int = 0
    parse_failed: int = 0
    resolved: int = 0
    unresolved_on_pm: int = 0
    fetch_failed: int = 0
    upserted: int = 0


def _fetch_candidates(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(_SHADOW_CANDIDATES_SQL)
        return cur.fetchall()


def _outcome_from_state(state: Optional[dict]) -> Optional[str]:
    """Map a Polymarket market-state dict to 'YES'/'NO'/None.

    Mirrors resolver._fetch_outcome: only resolved markets with a pinned
    midpoint (1.0 = YES won, 0.0 = NO won) yield a label.
    """
    if state is None or not state.get("is_resolved"):
        return None
    midpoint = state.get("midpoint_price")
    if midpoint == Decimal("1"):
        return "YES"
    if midpoint == Decimal("0"):
        return "NO"
    return None


def _resolve_one(client: PolymarketClient, row: dict) -> tuple[dict, Optional[str], bool]:
    """Return (enriched_row, outcome, fetch_ok).

    enriched_row gains asset_id / window_* from the slug. outcome is
    'YES'/'NO'/None. fetch_ok is False only on a hard fetch error (so the
    caller can distinguish "PM says not yet resolved" from "we failed").
    """
    try:
        asset_id, ws, we, dur = parse_short_horizon_slug(row["slug"])
    except ValueError as e:
        logger.debug("slug parse failed for %s: %s", row["slug"], e)
        return row, None, True  # not a fetch failure; just unparseable
    row = {**row, "asset_id": asset_id, "window_start_ts": ws,
           "window_end_ts": we, "window_duration_s": dur}
    try:
        state = client.fetch_market_state(row["polymarket_id"])
    except Exception as e:
        logger.warning("PM fetch error for %s: %s", row["polymarket_id"], e)
        return row, None, False
    return row, _outcome_from_state(state), True


def recover_shadow_labels(
    conninfo: str,
    max_workers: int = 8,
    limit: Optional[int] = None,
) -> RecoveryStats:
    """End-to-end recovery. Returns stats; upserts into quant_shadow_labels."""
    stats = RecoveryStats()
    client = PolymarketClient()
    try:
        with psycopg.connect(conninfo, row_factory=dict_row) as conn:
            candidates = _fetch_candidates(conn)
            if limit:
                candidates = candidates[:limit]
            stats.candidates = len(candidates)
            logger.info("recovering %s shadow candidates", stats.candidates)

            # Fetch outcomes concurrently; the client handles 429 backoff.
            results: list[tuple[dict, Optional[str]]] = []
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_resolve_one, client, r): r for r in candidates}
                for i, fut in enumerate(as_completed(futures), 1):
                    row, outcome, fetch_ok = fut.result()
                    if "asset_id" not in row:
                        stats.parse_failed += 1
                        continue
                    stats.parsed += 1
                    if not fetch_ok:
                        stats.fetch_failed += 1
                        continue
                    if outcome is None:
                        stats.unresolved_on_pm += 1
                    else:
                        stats.resolved += 1
                    results.append((row, outcome))
                    if i % 250 == 0:
                        logger.info("fetched %s/%s outcomes", i, stats.candidates)

            # Upsert everything we parsed (including unresolved rows, so a
            # later pass can fill them; outcome NULL is allowed).
            with conn.cursor() as cur:
                for row, outcome in results:
                    cur.execute(_UPSERT_SQL, {
                        "polymarket_id": row["polymarket_id"],
                        "slug": row["slug"],
                        "asset_id": row["asset_id"],
                        "decision_ts": row["decision_ts"],
                        "estimator_p_up": row["estimator_p_up"],
                        "spot_at_decision": row["spot_at_decision"],
                        "mid": row["mid"],
                        "vol_at_decision": row["vol_at_decision"],
                        "abs_edge": row["abs_edge"],
                        "window_start_ts": row["window_start_ts"],
                        "window_end_ts": row["window_end_ts"],
                        "window_duration_s": row["window_duration_s"],
                        "outcome": outcome,
                    })
                    stats.upserted += 1
            conn.commit()
    finally:
        client.close() if hasattr(client, "close") else None

    logger.info(
        "recovery done: candidates=%s parsed=%s resolved=%s unresolved_pm=%s "
        "fetch_failed=%s parse_failed=%s upserted=%s",
        stats.candidates, stats.parsed, stats.resolved, stats.unresolved_on_pm,
        stats.fetch_failed, stats.parse_failed, stats.upserted,
    )
    return stats

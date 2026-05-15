"""Tick-history features for the microstructure-estimator.

Computes VPIN (50-bucket toxicity) and BNS jump indicator per trade
by pulling a short rolling window of trade prints and mid prices from
the DB. Stage 1 SQL (scripts/extract_features_v2.sql) handles the
scalar features that don't need an array.

This module is referenced by the locked pre-registration
docs/feat/microstructure-estimator.md (commit d5466937). Changing the
feature definitions requires a new pre-registration doc.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd
import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("polyagent.quant.ml.features")


# --- Pure feature primitives (no I/O; testable on synthetic data) -----------

def vpin(sides: np.ndarray, sizes: np.ndarray, n_buckets: int = 50) -> float:
    """VPIN (Easley-Lopez de Prado-O'Hara 2012) over n_buckets equal-volume.

    Inputs are time-ordered. `sides` is +1 for buyer-aggressor, -1 for
    seller-aggressor. `sizes` is in contract or base-asset units.

    Returns the mean over buckets of |signed_volume| / bucket_volume in
    [0, 1]. Higher = more toxic / one-sided flow. NaN if insufficient
    trades to form n_buckets.
    """
    sides = np.asarray(sides, dtype=float)
    sizes = np.asarray(sizes, dtype=float)
    if len(sides) < n_buckets or sizes.sum() <= 0:
        return float("nan")
    cumvol = np.cumsum(sizes)
    total = cumvol[-1]
    bucket_size = total / n_buckets
    if bucket_size <= 0:
        return float("nan")
    # Bucket id for each trade; clip to last bucket to handle float drift.
    buckets = np.minimum((cumvol / bucket_size).astype(int), n_buckets - 1)
    signed = sides * sizes
    sum_signed = np.zeros(n_buckets)
    sum_vol = np.zeros(n_buckets)
    np.add.at(sum_signed, buckets, signed)
    np.add.at(sum_vol, buckets, sizes)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratios = np.where(sum_vol > 0, np.abs(sum_signed) / sum_vol, 0.0)
    return float(ratios.mean())


def bns_jump_ratio(returns: np.ndarray) -> float:
    """Barndorff-Nielsen / Shephard jump-detection ratio (RV - BV) / RV.

    RV = sum(r_i^2) — realized variance.
    BV = (pi/2) * sum(|r_{i-1}| * |r_i|) — bipower variation.

    Under no jumps, RV ≈ BV ⇒ ratio ≈ 0. Under jumps, RV > BV ⇒ ratio > 0.
    NaN if fewer than 3 returns.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 3:
        return float("nan")
    rv = float((r * r).sum())
    if rv <= 0:
        return 0.0
    bv = (math.pi / 2.0) * float((np.abs(r[:-1]) * np.abs(r[1:])).sum())
    return (rv - bv) / rv


# --- DB pulls --------------------------------------------------------------

@dataclass(frozen=True)
class TickFeatureConfig:
    """Window sizes for each tick-history feature."""
    vpin_lookback_seconds: int = 300
    vpin_buckets: int = 50
    jump_lookback_seconds: int = 300
    venue_prints: str = "bybit"
    product_prints: str = "BTCUSDT"
    venue_mids: str = "coinbase"
    product_mids: str = "BTC-USD"


_TRADE_PRINTS_SQL = """
    SELECT ts,
           CASE WHEN side IN ('buy', 'Buy') THEN 1 ELSE -1 END AS s,
           size
    FROM trade_prints
    WHERE venue = %(venue)s AND product = %(product)s
      AND ts BETWEEN %(start)s AND %(end)s
    ORDER BY ts
"""

_OB_MIDS_SQL = """
    SELECT mid
    FROM orderbook_snapshots
    WHERE venue = %(venue)s AND product = %(product)s
      AND ts BETWEEN %(start)s AND %(end)s
      AND mid > 0
    ORDER BY ts
"""


def compute_tick_features(
    conn: psycopg.Connection,
    decision_ts: pd.Timestamp,
    cfg: TickFeatureConfig = TickFeatureConfig(),
) -> dict[str, float]:
    """Compute VPIN and BNS jump-ratio for a single decision_ts.

    Pulls a focused 5-min window per feature. Designed to be called
    in a tight loop over trades from the orchestrator.
    """
    from datetime import timedelta

    vpin_start = decision_ts - timedelta(seconds=cfg.vpin_lookback_seconds)
    jump_start = decision_ts - timedelta(seconds=cfg.jump_lookback_seconds)

    with conn.cursor() as cur:
        cur.execute(_TRADE_PRINTS_SQL, {
            "venue": cfg.venue_prints, "product": cfg.product_prints,
            "start": vpin_start, "end": decision_ts,
        })
        prints = cur.fetchall()
        cur.execute(_OB_MIDS_SQL, {
            "venue": cfg.venue_mids, "product": cfg.product_mids,
            "start": jump_start, "end": decision_ts,
        })
        mids = [float(r["mid"]) for r in cur.fetchall()]

    if prints:
        sides = np.fromiter((r["s"] for r in prints), dtype=float)
        sizes = np.fromiter((float(r["size"]) for r in prints), dtype=float)
        v = vpin(sides, sizes, cfg.vpin_buckets)
    else:
        v = float("nan")

    if len(mids) >= 3:
        m = np.asarray(mids, dtype=float)
        returns = np.diff(np.log(m))
        j = bns_jump_ratio(returns)
    else:
        j = float("nan")

    return {"vpin_50": v, "jump_indicator_5m": j}


def compute_tick_features_for_trades(
    conn: psycopg.Connection,
    trades: Iterable[tuple[str, pd.Timestamp]],
    cfg: TickFeatureConfig = TickFeatureConfig(),
) -> pd.DataFrame:
    """Iterate over (trade_id, decision_ts) pairs, return a DataFrame.

    Single-threaded but uses connection-pooling-friendly semantics: each
    call reuses the same connection. Caller is responsible for connection
    lifetime.
    """
    rows = []
    for i, (trade_id, ts) in enumerate(trades, 1):
        if i % 500 == 0:
            logger.info("tick features: processed %s trades", i)
        feats = compute_tick_features(conn, ts, cfg)
        feats["trade_id"] = trade_id
        rows.append(feats)
    return pd.DataFrame(rows)

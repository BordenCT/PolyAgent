"""Feature extraction orchestrator.

Two stages:
  1. Run the locked Stage 1 SQL (scripts/extract_features_v2.sql) and
     load the wide CSV of scalar features.
  2. For each trade, pull a 5-min tick window and compute VPIN + BNS
     jump indicator via polyagent.services.quant.ml.features.

Output: a single wide CSV with the locked feature schema, ready for
walk-forward training.

Referenced by docs/feat/microstructure-estimator.md (commit d5466937).
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import psycopg
from psycopg.rows import dict_row

from polyagent.services.quant.ml.features import (
    TickFeatureConfig,
    compute_tick_features_for_trades,
)

logger = logging.getLogger("polyagent.quant.ml.extract")


REPO_ROOT = Path(__file__).resolve().parents[4]
STAGE1_SQL = REPO_ROOT / "scripts" / "extract_features_v2.sql"
SHADOW_SQL = REPO_ROOT / "scripts" / "extract_features_shadow.sql"


def run_stage1(conn: psycopg.Connection) -> pd.DataFrame:
    """Execute the locked Stage 1 SQL and return a DataFrame."""
    if not STAGE1_SQL.exists():
        raise FileNotFoundError(f"Stage 1 SQL missing: {STAGE1_SQL}")
    sql = STAGE1_SQL.read_text()
    logger.info("running Stage 1 feature extraction SQL")
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description] if cur.description else []
        rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=cols)
    if isinstance(rows[0], dict):
        df = pd.DataFrame(rows)
    else:
        df = pd.DataFrame(rows, columns=cols)
    df["decision_ts"] = pd.to_datetime(df["decision_ts"], utc=True)
    df["window_start_ts"] = pd.to_datetime(df["window_start_ts"], utc=True)
    df["window_end_ts"] = pd.to_datetime(df["window_end_ts"], utc=True)
    return df


def run_stage2(conn: psycopg.Connection, stage1: pd.DataFrame) -> pd.DataFrame:
    """Compute VPIN + jump indicator per trade. Returns trade_id-keyed DF."""
    logger.info("running Stage 2 tick-history features for %s trades", len(stage1))
    pairs = list(zip(stage1["trade_id"].astype(str), stage1["decision_ts"]))
    cfg = TickFeatureConfig()
    return compute_tick_features_for_trades(conn, pairs, cfg)


def run_shadow(conn: psycopg.Connection) -> pd.DataFrame:
    """Execute the shadow feature SQL (VPIN + jump computed in-SQL).

    Sources decision points from quant_shadow_labels, so no Python
    tick-feature stage is needed.
    """
    if not SHADOW_SQL.exists():
        raise FileNotFoundError(f"Shadow SQL missing: {SHADOW_SQL}")
    sql = SHADOW_SQL.read_text()
    logger.info("running shadow feature extraction SQL")
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description] if cur.description else []
        rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(rows) if isinstance(rows[0], dict) else pd.DataFrame(rows, columns=cols)
    for col in ("decision_ts", "window_start_ts", "window_end_ts"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], utc=True)
    return df


def extract_features(
    conninfo: str,
    out_csv: Path,
    source: str = "trades",
) -> pd.DataFrame:
    """End-to-end feature extraction.

    source='trades': locked Stage 1 SQL + Python Stage 2 tick features.
    source='shadow': recovered evaluated-market labels; VPIN + jump are
        computed in-SQL so no Python stage is needed.
    """
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(conninfo, row_factory=dict_row) as conn:
        if source == "shadow":
            merged = run_shadow(conn)
            if merged.empty:
                logger.warning("shadow extraction returned no rows; aborting")
                return merged
        else:
            stage1 = run_stage1(conn)
            if stage1.empty:
                logger.warning("Stage 1 returned no rows; aborting")
                return stage1
            stage2 = run_stage2(conn, stage1)
            merged = stage1.merge(stage2, on="trade_id", how="left")
    merged.to_csv(out_csv, index=False)
    logger.info("wrote %s rows to %s", len(merged), out_csv)
    return merged

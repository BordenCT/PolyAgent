"""End-to-end pipeline: features -> training -> report.

CLI entrypoint:
    python -m polyagent.services.quant.ml.pipeline \\
        --conninfo $DATABASE_URL \\
        --features-out /tmp/features.csv \\
        --report-out docs/feat/microstructure-estimator-report.md

The shell wrapper (scripts/microstructure_pipeline.sh) handles the
pre-fit gate check, idempotency, and git commit/push.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import pandas as pd

import psycopg
from psycopg.rows import dict_row

from polyagent.services.quant.ml.extract import extract_features
from polyagent.services.quant.ml.report import render_report
from polyagent.services.quant.ml.train import (
    MIN_JOINABLE_TRADES,
    train_walk_forward,
)


GATE_SQL = """
    SELECT COUNT(*)::int AS n
    FROM quant_short_v t
    WHERE t.pnl IS NOT NULL
      AND EXISTS (
          SELECT 1 FROM orderbook_snapshots ob
          WHERE ob.venue='bybit' AND ob.product='BTCUSDT'
            AND ob.ts <= t.decision_ts
            AND ob.ts >= t.decision_ts - INTERVAL '60 seconds'
      )
"""


def count_joinable_trades(conninfo: str) -> int:
    """Count resolved trades that have at least one matching OB snapshot."""
    with psycopg.connect(conninfo, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(GATE_SQL)
            row = cur.fetchone()
    return int(row["n"]) if row else 0

logger = logging.getLogger("polyagent.quant.ml.pipeline")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def derive_y_yes(df: pd.DataFrame) -> pd.DataFrame:
    """Add target column. Outcome is 'YES'/'NO' in the wide CSV."""
    if "y_yes" in df.columns:
        return df
    if "outcome" not in df.columns:
        raise ValueError("expected 'outcome' column in feature frame")
    df = df.copy()
    df["y_yes"] = (df["outcome"].astype(str) == "YES").astype(int)
    return df


def run(
    conninfo: str,
    features_out: Path,
    report_out: Path,
    report_json: Path | None = None,
    skip_extract: bool = False,
    source: str = "trades",
) -> int:
    """Returns exit code; 0 = success, non-zero = failure."""
    features_out = Path(features_out)
    report_out = Path(report_out)

    # Stage 1+2: extract features (skip if user pre-built features).
    if skip_extract and features_out.exists():
        logger.info("--skip-extract set; loading %s", features_out)
        df = pd.read_csv(features_out, parse_dates=["decision_ts", "window_start_ts", "window_end_ts"])
    else:
        df = extract_features(conninfo, features_out, source=source)

    if df.empty:
        logger.error("feature extraction produced no rows")
        return 2

    df = derive_y_yes(df)

    # Stage 3: walk-forward training.
    logger.info("starting walk-forward training over %s rows", len(df))
    report = train_walk_forward(df)

    logger.info(
        "training complete: decision=%s (n_joinable=%s, folds=%s)",
        report.overall_decision, report.n_joinable, len(report.folds),
    )

    # Stage 4: render markdown report.
    rendered = render_report(report, report_out)
    logger.info("wrote report to %s", rendered)

    if report_json is not None:
        # Light-weight JSON dump for downstream scripting.
        report_json = Path(report_json)
        report_json.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "decision": report.overall_decision,
            "rationale": report.decision_rationale,
            "n_joinable": report.n_joinable,
            "spec_commit": report.spec_commit,
            "folds": [
                {
                    "fold": fr.fold,
                    "train_n": fr.train_n, "test_n": fr.test_n,
                    "train_end": fr.train_end.isoformat() if fr.train_end is not None else None,
                    "test_start": fr.test_start.isoformat() if fr.test_start is not None else None,
                    "median_brier": fr.median_brier,
                    "median_res": fr.median_res,
                    "best_brier": fr.best_brier,
                }
                for fr in report.folds
            ],
        }
        report_json.write_text(json.dumps(payload, indent=2, default=str))
        logger.info("wrote JSON summary to %s", report_json)

    if report.n_joinable < MIN_JOINABLE_TRADES:
        return 3  # gate not met
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--conninfo",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres connection string (defaults to $DATABASE_URL).",
    )
    parser.add_argument(
        "--features-out",
        default="quant_short_features.csv",
        type=Path,
        help="Where to write the wide feature CSV.",
    )
    parser.add_argument(
        "--report-out",
        default="docs/feat/microstructure-estimator-report.md",
        type=Path,
        help="Where to write the markdown report.",
    )
    parser.add_argument(
        "--report-json",
        default="docs/feat/microstructure-estimator-report.json",
        type=Path,
        help="Where to write a lightweight JSON summary alongside the markdown.",
    )
    parser.add_argument(
        "--skip-extract",
        action="store_true",
        help="Skip feature extraction; load --features-out directly.",
    )
    parser.add_argument(
        "--gate-check-only",
        action="store_true",
        help="Print joinable trade count and exit 0 if gate met, 3 if not.",
    )
    parser.add_argument(
        "--source",
        choices=["trades", "shadow"],
        default="trades",
        help="Feature source: 'trades' (locked Stage 1+2) or 'shadow' "
             "(recovered evaluated-market labels; VPIN/jump in-SQL).",
    )
    parser.add_argument(
        "--recover-first",
        action="store_true",
        help="Run shadow-label recovery (backfill quant_shadow_labels from "
             "Polymarket) before extracting. Only meaningful with --source shadow.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)
    if not args.conninfo and not args.skip_extract:
        print("DATABASE_URL is required (set env var or pass --conninfo)", file=sys.stderr)
        return 4

    if args.gate_check_only:
        n = count_joinable_trades(args.conninfo or "")
        print(f"joinable_trades={n} required={MIN_JOINABLE_TRADES}")
        return 0 if n >= MIN_JOINABLE_TRADES else 3

    # Optional: backfill shadow labels from Polymarket before extracting.
    if args.recover_first:
        if args.source != "shadow":
            logger.warning("--recover-first is only meaningful with --source shadow")
        from polyagent.services.quant.ml.recover import recover_shadow_labels
        logger.info("running shadow-label recovery")
        stats = recover_shadow_labels(args.conninfo or "")
        logger.info("recovery stats: %s", stats)

    # Early gate check (trades source only; shadow relies on the
    # in-training MIN_JOINABLE_TRADES check against the recovered set).
    if args.source == "trades" and not args.skip_extract and args.conninfo:
        try:
            n = count_joinable_trades(args.conninfo)
            if n < MIN_JOINABLE_TRADES:
                logger.info(
                    "gate not met: %s joinable < %s required; exiting",
                    n, MIN_JOINABLE_TRADES,
                )
                return 3
        except Exception as e:
            logger.warning("gate pre-check failed: %s; proceeding to extract", e)

    return run(
        conninfo=args.conninfo or "",
        features_out=args.features_out,
        report_out=args.report_out,
        report_json=args.report_json,
        skip_extract=args.skip_extract,
        source=args.source,
    )


if __name__ == "__main__":
    sys.exit(main())

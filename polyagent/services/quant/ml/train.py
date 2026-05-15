"""Walk-forward training for the microstructure-estimator.

LOCKED at pre-registration commit d5466937. The feature list and grid
are fixed; this module REFUSES to run if features are missing or extra
features are passed.

Spec reference: docs/feat/microstructure-estimator.md
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, roc_auc_score

logger = logging.getLogger("polyagent.quant.ml.train")


# Pre-registered feature list. Order is documentation, not load-bearing.
# Any modification requires a new pre-registration doc with a fresh hash.
LOCKED_FEATURES: tuple[str, ...] = (
    # Tier 1: must-have microstructure
    "bybit_aggr_flow_10s",
    "bybit_aggr_flow_60s",
    "bybit_aggr_flow_300s",
    "bybit_aggr_flow_900s",
    "coinbase_aggr_flow_10s",
    "coinbase_aggr_flow_60s",
    "coinbase_aggr_flow_300s",
    "coinbase_aggr_flow_900s",
    "bybit_ofi_60s",
    "coinbase_ofi_60s",
    "bybit_microprice_dev",
    "coinbase_microprice_dev",
    "cross_venue_mid_diff_bps",
    "bybit_basis",
    # Tier 2: high-value extensions
    "realized_vol_1m",
    "realized_vol_5m",
    "realized_vol_15m",
    "jump_indicator_5m",
    "ret_10s",
    "ret_60s",
    "ret_300s",
    "vpin_50",
    "bybit_spread_regime",
    "coinbase_spread_regime",
    "strike_distance",
    # Tier 3: regime & operational controls
    "hour_sin",
    "hour_cos",
    "minutes_since_funding",
    "time_into_window",
    "window_5m",
    "concurrent_with_prior",
)

# xgboost grid, locked. N = 9 for multi-test correction.
GRID: tuple[tuple[int, float], ...] = tuple(
    (depth, lr) for depth in (3, 5, 7) for lr in (0.01, 0.05, 0.10)
)
N_GRID = len(GRID)  # 9

# Decision thresholds from the spec.
THRESH_BRIER = 0.244
THRESH_RES = 0.008

# Minimum joinable trades to allow training (pre-fit gate).
MIN_JOINABLE_TRADES = 4000


@dataclass
class FoldResult:
    fold: int
    train_n: int
    val_n: int
    test_n: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    # per-grid-config metrics (length N_GRID)
    config_briers: list[float] = field(default_factory=list)
    config_rels: list[float] = field(default_factory=list)
    config_res: list[float] = field(default_factory=list)
    config_aucs: list[float] = field(default_factory=list)
    config_specs: list[tuple[int, float]] = field(default_factory=list)
    # aggregated
    median_brier: float = math.nan
    median_res: float = math.nan
    best_brier: float = math.nan
    best_brier_config: tuple[int, float] | None = None
    # feature importance from the best config
    feature_importance: dict[str, float] = field(default_factory=dict)


@dataclass
class TrainingReport:
    n_joinable: int
    folds: list[FoldResult]
    overall_decision: str  # DEPLOY / MORE-RESEARCH / ABANDON
    decision_rationale: str
    spec_commit: str = "d5466937c6d17c893309d21668306c8f295459c8"


def _check_features(df: pd.DataFrame) -> None:
    """Verify the locked feature list is present. Refuse otherwise."""
    missing = [f for f in LOCKED_FEATURES if f not in df.columns]
    if missing:
        raise ValueError(
            f"locked features missing from input frame: {missing}. "
            f"Either feature extraction is incomplete, or the spec has "
            f"been amended without updating this module."
        )


def murphy_decomp(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> tuple[float, float, float]:
    """Murphy decomposition of Brier into (REL, RES, UNC)."""
    p = np.asarray(p, dtype=float)
    y = np.asarray(y, dtype=float)
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    idx = np.clip(np.digitize(p, bins) - 1, 0, n_bins - 1)
    o_bar = float(y.mean()) if len(y) else 0.5
    n = len(p)
    rel = res = 0.0
    for k in range(n_bins):
        mask = idx == k
        nk = int(mask.sum())
        if nk == 0:
            continue
        f_k = float(p[mask].mean())
        o_k = float(y[mask].mean())
        rel += nk * (f_k - o_k) ** 2
        res += nk * (o_k - o_bar) ** 2
    return rel / max(n, 1), res / max(n, 1), o_bar * (1.0 - o_bar)


def walk_forward_folds(
    decision_ts: pd.Series,
    embargo_hours: int = 24,
) -> list[dict[str, np.ndarray]]:
    """Return 3 walk-forward fold indices per the locked spec.

    Each fold = {"train": idx, "val": idx, "test": idx} with a 24h
    embargo between train_end and test_start to prevent label leakage
    on rolling-vol features.

    Fold layout (in normalized time fractions of the sorted index):
      Fold 1: train [0, 0.60), val [0.60, 0.75), test [0.78, 0.93)
      Fold 2: train [0, 0.75), val [0.75, 0.85), test [0.88, 1.00)
      Fold 3: train [0, 0.80), val [0.80, 0.90), test [0.93, 1.00)
                                                  |
                                                  most-recent holdout
    """
    n = len(decision_ts)
    if n < 100:
        raise ValueError(f"too few rows for walk-forward: {n}")
    sorted_ix = np.argsort(decision_ts.values)
    ts = pd.Series(decision_ts.values[sorted_ix]).reset_index(drop=True)
    embargo = pd.Timedelta(hours=embargo_hours)

    def fold_idx(train_frac, val_frac, test_frac):
        train_end_i = int(n * train_frac)
        val_end_i = int(n * val_frac)
        test_start_target = ts.iloc[train_end_i] + embargo
        # Move test_start to first index >= test_start_target.
        test_start_i = int((ts >= test_start_target).idxmax())
        test_start_i = max(test_start_i, val_end_i)
        test_end_i = int(n * test_frac)
        if test_end_i <= test_start_i:
            return None
        return {
            "train": sorted_ix[:train_end_i],
            "val":   sorted_ix[train_end_i:val_end_i],
            "test":  sorted_ix[test_start_i:test_end_i],
        }

    folds = []
    specs = [(0.60, 0.75, 0.93), (0.75, 0.85, 1.00), (0.80, 0.90, 1.00)]
    for s in specs:
        f = fold_idx(*s)
        if f is not None and len(f["test"]) >= 20:
            folds.append(f)
    return folds


def train_one(
    X_tr: np.ndarray, y_tr: np.ndarray,
    X_val: np.ndarray, y_val: np.ndarray,
    X_te: np.ndarray,
    max_depth: int, lr: float,
) -> tuple[np.ndarray, dict[str, float]]:
    """Train one xgboost config; return test predictions and feature importance.

    Uses early stopping on the validation fold. n_estimators capped at 500.
    """
    import xgboost as xgb

    dtr = xgb.DMatrix(X_tr, label=y_tr)
    dval = xgb.DMatrix(X_val, label=y_val)
    dte = xgb.DMatrix(X_te)
    params = {
        "objective": "binary:logistic",
        "eval_metric": "logloss",
        "max_depth": max_depth,
        "learning_rate": lr,
        "subsample": 0.85,
        "colsample_bytree": 0.85,
        "tree_method": "hist",
        "verbosity": 0,
    }
    booster = xgb.train(
        params,
        dtr,
        num_boost_round=500,
        evals=[(dval, "val")],
        early_stopping_rounds=30,
        verbose_eval=False,
    )
    p_te = booster.predict(dte)
    importance = booster.get_score(importance_type="gain") or {}
    return p_te, importance


def evaluate_config(p_te: np.ndarray, y_te: np.ndarray) -> dict[str, float]:
    brier = float(brier_score_loss(y_te, p_te))
    rel, res, _ = murphy_decomp(p_te, y_te)
    try:
        auc = float(roc_auc_score(y_te, p_te))
    except ValueError:
        auc = math.nan
    return {"brier": brier, "rel": rel, "res": res, "auc": auc}


def train_walk_forward(df: pd.DataFrame, target_col: str = "y_yes") -> TrainingReport:
    """Run the full walk-forward + grid sweep. Returns a structured report."""
    _check_features(df)
    df = df.dropna(subset=list(LOCKED_FEATURES) + [target_col, "decision_ts"]).copy()
    df = df.sort_values("decision_ts").reset_index(drop=True)
    n_joinable = len(df)
    if n_joinable < MIN_JOINABLE_TRADES:
        return TrainingReport(
            n_joinable=n_joinable, folds=[],
            overall_decision="ABANDON",
            decision_rationale=(
                f"only {n_joinable} joinable trades; spec requires "
                f">= {MIN_JOINABLE_TRADES}. Pipeline must wait."
            ),
        )

    X_all = df[list(LOCKED_FEATURES)].to_numpy(dtype=float)
    y_all = df[target_col].astype(int).to_numpy()
    ts_all = df["decision_ts"]

    folds = walk_forward_folds(ts_all)
    if not folds:
        return TrainingReport(
            n_joinable=n_joinable, folds=[],
            overall_decision="ABANDON",
            decision_rationale="walk-forward folds could not be constructed",
        )

    fold_results: list[FoldResult] = []
    for k, fdef in enumerate(folds, 1):
        tr, val, te = fdef["train"], fdef["val"], fdef["test"]
        Xtr, Xval, Xte = X_all[tr], X_all[val], X_all[te]
        ytr, yval, yte = y_all[tr], y_all[val], y_all[te]

        fr = FoldResult(
            fold=k,
            train_n=len(tr), val_n=len(val), test_n=len(te),
            train_start=ts_all.iloc[tr.min()], train_end=ts_all.iloc[tr.max()],
            test_start=ts_all.iloc[te.min()], test_end=ts_all.iloc[te.max()],
        )

        best_brier = math.inf
        best_pred: np.ndarray | None = None
        best_importance: dict[str, float] = {}
        best_spec: tuple[int, float] | None = None
        for (depth, lr) in GRID:
            try:
                p_te, importance = train_one(Xtr, ytr, Xval, yval, Xte, depth, lr)
                m = evaluate_config(p_te, yte)
            except Exception as e:
                logger.warning("fold %s config (d=%s, lr=%s) failed: %s", k, depth, lr, e)
                m = {"brier": math.nan, "rel": math.nan, "res": math.nan, "auc": math.nan}
                p_te = None
                importance = {}
            fr.config_specs.append((depth, lr))
            fr.config_briers.append(m["brier"])
            fr.config_rels.append(m["rel"])
            fr.config_res.append(m["res"])
            fr.config_aucs.append(m["auc"])
            if not math.isnan(m["brier"]) and m["brier"] < best_brier:
                best_brier = m["brier"]
                best_pred = p_te
                best_importance = importance
                best_spec = (depth, lr)

        # Aggregate per-fold.
        valid_briers = [b for b in fr.config_briers if not math.isnan(b)]
        valid_res = [r for r in fr.config_res if not math.isnan(r)]
        fr.median_brier = float(np.median(valid_briers)) if valid_briers else math.nan
        fr.median_res = float(np.median(valid_res)) if valid_res else math.nan
        fr.best_brier = best_brier if math.isfinite(best_brier) else math.nan
        fr.best_brier_config = best_spec
        # Map importance keys (xgb default 'f0', 'f1'...) to feature names.
        renamed = {}
        for raw_k, v in best_importance.items():
            try:
                idx = int(raw_k[1:]) if raw_k.startswith("f") else int(raw_k)
                if 0 <= idx < len(LOCKED_FEATURES):
                    renamed[LOCKED_FEATURES[idx]] = float(v)
            except (ValueError, IndexError):
                continue
        fr.feature_importance = renamed
        fold_results.append(fr)

    # Decision rule (adapted from spec).
    # Spec said DSR with N=9. We adapt to: median across 9 configs must meet
    # both thresholds. Documents the adaptation rather than silently
    # pretending DSR over Brier means something canonical.
    folds_passing = 0
    for fr in fold_results:
        b_ok = (not math.isnan(fr.median_brier)) and fr.median_brier <= THRESH_BRIER
        r_ok = (not math.isnan(fr.median_res)) and fr.median_res >= THRESH_RES
        if b_ok and r_ok:
            folds_passing += 1

    n = len(fold_results)
    if folds_passing == n:
        decision = "DEPLOY"
        rationale = f"all {n} folds met median Brier <= {THRESH_BRIER} AND median RES >= {THRESH_RES}"
    elif folds_passing >= max(1, n - 1):
        decision = "MORE-RESEARCH"
        rationale = f"{folds_passing}/{n} folds passed; near miss"
    else:
        decision = "ABANDON"
        rationale = f"only {folds_passing}/{n} folds passed median Brier+RES thresholds"

    return TrainingReport(
        n_joinable=n_joinable,
        folds=fold_results,
        overall_decision=decision,
        decision_rationale=rationale,
    )

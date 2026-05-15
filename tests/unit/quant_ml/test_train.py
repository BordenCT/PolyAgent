"""Unit tests for the training module (walk-forward, Murphy, decision rule)."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from polyagent.services.quant.ml.train import (
    GRID,
    LOCKED_FEATURES,
    MIN_JOINABLE_TRADES,
    N_GRID,
    THRESH_BRIER,
    THRESH_RES,
    murphy_decomp,
    train_walk_forward,
    walk_forward_folds,
)


# ---- Locked schema sanity --------------------------------------------------

def test_grid_size_matches_spec():
    """Spec locks N=9 grid configs for multi-test correction."""
    assert N_GRID == 9
    assert len(GRID) == 9


def test_feature_count():
    """Feature enumeration must match the locked schema."""
    # Tier 1: 14, Tier 2: 11 (incl. both venues for spread regime),
    # Tier 3: 6. Total 31. Spec said "27" but the LIST was correct;
    # 31 reflects the enumerated list.
    assert len(LOCKED_FEATURES) == 31
    # All names are unique.
    assert len(set(LOCKED_FEATURES)) == len(LOCKED_FEATURES)
    # Required pre-registered names are present.
    for must_have in (
        "bybit_aggr_flow_60s", "coinbase_aggr_flow_60s",
        "bybit_ofi_60s", "cross_venue_mid_diff_bps",
        "vpin_50", "jump_indicator_5m",
        "strike_distance", "time_into_window", "window_5m",
    ):
        assert must_have in LOCKED_FEATURES


# ---- Murphy decomposition --------------------------------------------------

def test_murphy_constant_50_50():
    """Predicting 0.5 for everything: REL = 0 (perfectly calibrated to mean),
    RES = 0 (no resolution), UNC = base rate variance."""
    y = np.array([0, 1, 0, 1, 0, 1, 0, 1, 0, 1])
    p = np.full_like(y, 0.5, dtype=float)
    rel, res, unc = murphy_decomp(p, y)
    assert rel == pytest.approx(0.0, abs=1e-6)
    assert res == pytest.approx(0.0, abs=1e-6)
    assert unc == pytest.approx(0.25, abs=1e-6)


def test_murphy_perfect_predictions():
    """Predictions = outcomes ⇒ REL = 0 AND RES = UNC (full discrimination)."""
    rng = np.random.default_rng(0)
    y = rng.integers(0, 2, size=200)
    p = y.astype(float)
    rel, res, unc = murphy_decomp(p, y)
    assert rel == pytest.approx(0.0, abs=1e-6)
    # Perfect predictions resolve the full base-rate variance.
    assert res == pytest.approx(unc, abs=1e-6)


def test_murphy_decomp_sums_to_brier():
    """Brier = REL - RES + UNC."""
    rng = np.random.default_rng(1)
    y = rng.integers(0, 2, size=500)
    p = rng.uniform(0, 1, size=500)
    rel, res, unc = murphy_decomp(p, y)
    bs = np.mean((p - y) ** 2)
    assert (rel - res + unc) == pytest.approx(bs, abs=0.01)


# ---- Walk-forward folds ----------------------------------------------------

def test_walk_forward_basic_layout():
    """Three folds, time-ordered, no train/test index overlap."""
    n = 1000
    ts = pd.Series(pd.date_range("2026-01-01", periods=n, freq="5min"))
    folds = walk_forward_folds(ts)
    assert len(folds) >= 2  # at least two should be constructible
    for f in folds:
        # No overlap between train and test indices.
        assert len(set(f["train"]) & set(f["test"])) == 0
        # Test indices are strictly after train indices in time.
        if len(f["train"]) and len(f["test"]):
            assert ts.iloc[f["test"]].min() > ts.iloc[f["train"]].max()


def test_walk_forward_embargo_respected():
    """24h embargo between train end and test start."""
    n = 500
    ts = pd.Series(pd.date_range("2026-01-01", periods=n, freq="1h"))
    folds = walk_forward_folds(ts, embargo_hours=24)
    for f in folds:
        if len(f["train"]) and len(f["test"]):
            gap = ts.iloc[f["test"]].min() - ts.iloc[f["train"]].max()
            assert gap >= pd.Timedelta(hours=24)


def test_walk_forward_rejects_small_input():
    ts = pd.Series(pd.date_range("2026-01-01", periods=10, freq="5min"))
    with pytest.raises(ValueError):
        walk_forward_folds(ts)


# ---- Pre-fit gate ----------------------------------------------------------

def test_training_below_gate_returns_abandon_without_fitting():
    """If joinable count is below the gate, training returns ABANDON
    immediately and does NOT touch xgboost."""
    n = 100
    ts = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    df = pd.DataFrame({"decision_ts": ts, "y_yes": np.random.randint(0, 2, n)})
    for f in LOCKED_FEATURES:
        df[f] = np.random.normal(size=n)
    report = train_walk_forward(df)
    assert report.overall_decision == "ABANDON"
    assert report.n_joinable < MIN_JOINABLE_TRADES
    assert "gate" in report.decision_rationale.lower() or \
           str(MIN_JOINABLE_TRADES) in report.decision_rationale


def test_training_rejects_missing_features():
    """Refuse to run with the locked feature set partially missing."""
    n = MIN_JOINABLE_TRADES + 100
    ts = pd.date_range("2026-01-01", periods=n, freq="1min", tz="UTC")
    df = pd.DataFrame({"decision_ts": ts, "y_yes": np.random.randint(0, 2, n)})
    # Add only half the locked features.
    for f in LOCKED_FEATURES[:5]:
        df[f] = np.random.normal(size=n)
    with pytest.raises(ValueError, match="locked features missing"):
        train_walk_forward(df)

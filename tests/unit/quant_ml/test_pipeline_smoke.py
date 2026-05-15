"""End-to-end smoke test on synthetic data.

Runs the training + reporting pipeline on a small synthetic frame to
verify the components wire together. NOT testing model quality; we
expect ABANDON or noise-quality output.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from polyagent.services.quant.ml.report import render_report
from polyagent.services.quant.ml.train import (
    LOCKED_FEATURES,
    MIN_JOINABLE_TRADES,
    train_walk_forward,
)


def _synth_features(n: int, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2026-04-01", periods=n, freq="5min", tz="UTC")
    df = pd.DataFrame({
        "decision_ts": ts,
        "y_yes": rng.integers(0, 2, n),
    })
    for f in LOCKED_FEATURES:
        # Mix continuous and binary features to match real distributions.
        if f in {"window_5m", "concurrent_with_prior"}:
            df[f] = rng.integers(0, 2, n)
        else:
            df[f] = rng.normal(size=n)
    return df


@pytest.mark.slow
def test_pipeline_smoke_runs_without_crash(tmp_path):
    """Pipeline completes on minimum-size dataset and produces a report file."""
    df = _synth_features(MIN_JOINABLE_TRADES + 200)
    report = train_walk_forward(df)
    assert report.n_joinable >= MIN_JOINABLE_TRADES
    assert report.overall_decision in {"DEPLOY", "MORE-RESEARCH", "ABANDON"}
    assert len(report.folds) >= 1
    # Each fold has 9 grid configs evaluated.
    for fr in report.folds:
        assert len(fr.config_briers) == 9
        assert len(fr.config_res) == 9

    out = tmp_path / "report.md"
    rendered = render_report(report, out)
    assert rendered.exists()
    text = rendered.read_text()
    assert "Microstructure Estimator" in text
    assert "Decision" in text
    # Locked feature list appears.
    assert "`bybit_aggr_flow_60s`" in text


def test_pipeline_on_pure_noise_returns_abandon_or_research(tmp_path):
    """Synthetic random features cannot have predictive signal; expect
    a non-DEPLOY decision."""
    df = _synth_features(MIN_JOINABLE_TRADES + 200, seed=7)
    report = train_walk_forward(df)
    assert report.overall_decision in {"MORE-RESEARCH", "ABANDON"}

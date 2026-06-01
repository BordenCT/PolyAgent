"""Render a TrainingReport to a markdown file."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from polyagent.services.quant.ml.train import (
    LOCKED_FEATURES,
    N_GRID,
    THRESH_BRIER,
    THRESH_RES,
    FoldResult,
    TrainingReport,
)


def _fmt(x: float, digits: int = 4) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or not math.isfinite(x))):
        return "—"
    return f"{x:.{digits}f}"


def _fold_table(folds: Iterable[FoldResult]) -> str:
    folds = list(folds)
    head = (
        "| Fold | Train n | Test n | Train end | Test start | Median Brier | Median RES | Best Brier | Best config |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
    )
    rows = []
    for fr in folds:
        cfg = "—" if fr.best_brier_config is None else f"d={fr.best_brier_config[0]}, lr={fr.best_brier_config[1]}"
        rows.append(
            f"| {fr.fold} | {fr.train_n} | {fr.test_n} | "
            f"{fr.train_end} | {fr.test_start} | "
            f"{_fmt(fr.median_brier)} | {_fmt(fr.median_res)} | "
            f"{_fmt(fr.best_brier)} | {cfg} |"
        )
    return head + "\n".join(rows) + "\n"


def _config_grid_table(folds: Iterable[FoldResult]) -> str:
    folds = list(folds)
    if not folds:
        return ""
    # Use config order from first fold as canonical.
    fr0 = folds[0]
    head = "| Config | "
    head += " | ".join(f"Fold {fr.fold} Brier" for fr in folds)
    head += " | "
    head += " | ".join(f"Fold {fr.fold} RES" for fr in folds)
    head += " |\n|"
    head += "---|" * (1 + 2 * len(folds))
    head += "\n"
    rows = []
    for i, spec in enumerate(fr0.config_specs):
        row = f"| d={spec[0]}, lr={spec[1]} | "
        row += " | ".join(_fmt(fr.config_briers[i]) for fr in folds)
        row += " | "
        row += " | ".join(_fmt(fr.config_res[i]) for fr in folds)
        row += " |"
        rows.append(row)
    return head + "\n".join(rows) + "\n"


def _feature_importance_section(folds: Iterable[FoldResult]) -> str:
    out_lines = []
    for fr in folds:
        if not fr.feature_importance:
            continue
        ranked = sorted(fr.feature_importance.items(), key=lambda kv: -kv[1])[:10]
        out_lines.append(f"\n**Fold {fr.fold} top features (gain):**\n")
        for name, gain in ranked:
            out_lines.append(f"- `{name}`: {gain:.3f}")
    return "\n".join(out_lines) + "\n" if out_lines else ""


def render_report(report: TrainingReport, out_path: Path, source: str = "trades") -> Path:
    """Write the report to a markdown file. Returns the path."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ts_now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    parts: list[str] = []
    parts.append(f"# Microstructure Estimator — Training Report\n")
    parts.append(f"**Generated:** {ts_now}")
    parts.append(f"**Spec commit (pre-registered):** `{report.spec_commit}`")
    parts.append(f"**N joinable trades:** {report.n_joinable}")
    parts.append(f"**Decision:** **{report.overall_decision}**")
    parts.append(f"**Rationale:** {report.decision_rationale}\n")

    parts.append("## Inference Report Block\n")
    parts.append("```")
    parts.append("Claim: xgboost on locked microstructure features beats the lognormal")
    parts.append("       Phi(d2) baseline and exceeds Brier <= "
                 f"{THRESH_BRIER} AND RES >= {THRESH_RES} on all OOS folds.")
    universe = (
        "recovered shadow decision points (evaluated-but-not-traded "
        "*-updown-* markets), PM-settled, joined to bybit + coinbase market_data"
        if source == "shadow"
        else "BTC quant_short trades joined to bybit + coinbase market_data"
    )
    parts.append(f"Universe: {universe}.")
    parts.append(f"N joinable: {report.n_joinable}")
    if report.folds:
        f = report.folds[0]
        parts.append(f"Validation: 3-fold walk-forward, 24h embargo, time-ordered, no shuffle.")
        parts.append(f"Multi-test: N={N_GRID} grid configs; median across configs reported per fold.")
        parts.append(f"Mechanism: order-flow imbalance + cross-venue lead/lag + vol regime + funding cycle.")
        parts.append(f"Pre-registration: docs/feat/microstructure-estimator.md @ {report.spec_commit}")
    parts.append(f"Decision: {report.overall_decision}   Rationale: {report.decision_rationale}")
    parts.append("```\n")

    parts.append("## Per-fold metrics\n")
    parts.append(_fold_table(report.folds))

    parts.append("## Full grid x fold matrix\n")
    parts.append(_config_grid_table(report.folds))

    parts.append("## Feature importance (gain) from best-Brier config per fold\n")
    parts.append(_feature_importance_section(report.folds))

    parts.append("## Locked feature list\n")
    parts.append("These features were pre-registered. Any modification requires a "
                 "fresh pre-registration with a new commit hash.\n")
    for f in LOCKED_FEATURES:
        parts.append(f"- `{f}`")
    parts.append("")

    parts.append("## Thresholds\n")
    parts.append(f"- Brier (median across {N_GRID} configs, per fold) must be ≤ **{THRESH_BRIER}**")
    parts.append(f"- Resolution (median across {N_GRID} configs, per fold) must be ≥ **{THRESH_RES}**")
    parts.append(f"- DEPLOY: both thresholds met on **all** folds")
    parts.append(f"- MORE-RESEARCH: passed on n-1 of n folds")
    parts.append(f"- ABANDON: passed on fewer than n-1 folds\n")

    out_path.write_text("\n".join(parts))
    return out_path

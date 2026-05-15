# Microstructure Estimator — Runbook

**Spec:** `docs/feat/microstructure-estimator.md` (locked at commit `d5466937`)
**Author:** Charles Borden
**Last updated:** 2026-05-15

This is the "you walked back to your laptop after a trip, where do you look" guide for the auto-running microstructure pipeline.

---

## tl;dr — when you get back

```bash
cd ~/Development/PolyAgent     # or wherever the repo lives on the remote
git pull                       # the pipeline auto-commits its report
cat docs/feat/microstructure-estimator-report.md
```

If the report file exists, the pipeline ran successfully and the decision (DEPLOY / MORE-RESEARCH / ABANDON) is at the top.

If the report file does not exist, look at `microstructure_cron.log` (most recent gate-check / run attempt) and `microstructure_pipeline.log` (most recent actual run).

---

## How the pipeline runs

`scripts/microstructure_pipeline.sh` does the whole flow:

1. **Gate check.** Counts joinable trades (`>= 4000` required per spec). Exits cleanly if not met.
2. **Idempotency check.** If `docs/feat/microstructure-estimator-report.md` already exists, skips. To force a re-run, delete it.
3. **Lock file.** Creates `.training_in_progress`. Stale locks (>6h) are auto-cleared.
4. **Feature extraction.** Runs `scripts/extract_features_v2.sql` (Stage 1 scalars) and `polyagent.services.quant.ml.features.compute_tick_features_for_trades` (Stage 2 VPIN + BNS jump). Writes `quant_short_features.csv` (gitignored).
5. **Walk-forward training.** `polyagent.services.quant.ml.train.train_walk_forward` runs the locked xgboost N=9 grid over 3 walk-forward folds (24h embargo).
6. **Report.** Writes `docs/feat/microstructure-estimator-report.md` and `microstructure-estimator-report.json`. Includes per-fold metrics, full grid x fold matrix, top feature importances per fold, the locked feature list, and the DEPLOY / MORE-RESEARCH / ABANDON decision.
7. **Commit + push.** Auto-commit message: `chore(microstructure): auto-generated training report (<DECISION>)`.

---

## Cron setup (one-time, on the remote)

```bash
./scripts/install_microstructure_cron.sh install
```

This adds a `0 */6 * * *` entry pointing at the pipeline (runs every 6 hours). The pipeline is idempotent so re-running every 6h is safe; it will skip if the report already exists.

To check / remove:
```bash
./scripts/install_microstructure_cron.sh status
./scripts/install_microstructure_cron.sh remove
```

---

## Pre-fit gate timing

- Joinable trades as of 2026-05-15: **1571** (out of 2549 total resolved).
- Required: **4000**.
- Bot generates ~300 resolved trades/day, all joinable now that the market-data feeds are live.
- Expected gate-clear date: **~2026-05-23**.

Once the gate clears, the next cron tick runs the full pipeline (15-30 min of compute, dominated by feature-extraction lateral joins).

---

## What to look for in the report

1. **Top of file:** `Decision: <DEPLOY | MORE-RESEARCH | ABANDON>` and one-line rationale.
2. **Inference Report Block.** The standard form (claim, universe, N, validation, multi-test posture, decision).
3. **Per-fold metrics table.** Each fold's train/test span, median Brier across the 9 grid configs, median RES, best Brier, and which (max_depth, lr) won.
4. **Full grid x fold matrix.** Every (config, fold) cell, so the multi-test surface is visible without re-running.
5. **Feature importance (gain) for best config per fold.** Helps you see whether the same features matter across folds (robust signal) or each fold picks different ones (overfitting).
6. **Locked feature list.** Last sanity check that the code matches the spec.

---

## Decision rules (from spec)

| Outcome | Definition | Action |
|---|---|---|
| **DEPLOY** | Median Brier across 9 configs ≤ 0.244 AND median Resolution ≥ 0.008 on **all 3** folds | Quarter-Kelly paper shadow for 2 weeks, then promote |
| **MORE-RESEARCH** | Passed on 2 of 3 folds, or partial metrics | Collect 2 more weeks; re-run this same spec, no changes |
| **ABANDON** | Failed on majority of folds | Mothball `quant_short` directional prediction; restrict capital to main flow |

The spec adapts DSR (Sharpe-based) to a median-across-grid criterion since DSR over Brier scores has no canonical interpretation. The adaptation is documented in `polyagent/services/quant/ml/train.py:train_walk_forward`.

---

## Failure modes & where to look

| Symptom | Likely cause | Fix |
|---|---|---|
| Report missing, log says "gate not met" | Bot hasn't generated 4000 joinable trades yet | Wait. Or check why bot isn't producing trades (`polyagent status`). |
| Report missing, log shows "psql: could not connect" | DB container down or hostname unreachable | `podman compose up -d`. The pipeline auto-falls back to `localhost:5432`. |
| Lock file lingering | Previous run crashed | If `<6h` old: wait. If `>6h`: deleted automatically next tick. To force: `rm .training_in_progress`. |
| Report exists but decision = ABANDON | Microstructure features genuinely don't predict at this horizon | Per spec, mothball the subsystem. Don't keep iterating without a new pre-registration. |
| Pipeline ran but `quant_short_features.csv` is empty | Stage 1 SQL returned 0 rows; DB connection works but `quant_short_v` has no resolved trades. | Check `polyagent status` for resolved trade count. |

---

## What NOT to do

- Don't edit `polyagent/services/quant/ml/train.py::LOCKED_FEATURES` after the fact; that's a spec change requiring re-registration.
- Don't tweak the grid (`GRID`) or thresholds (`THRESH_BRIER`, `THRESH_RES`) after seeing results. If you do, the existing report becomes exploratory and a new pre-registration is needed.
- Don't run the pipeline manually after seeing one fold's result and discarding it. The cron job is idempotent and bound to the locked spec on purpose.

---

## Files reference

| Path | Purpose |
|---|---|
| `docs/feat/microstructure-estimator.md` | Pre-registration (LOCKED) |
| `docs/feat/microstructure-estimator.canvas` | Visual flowchart |
| `docs/feat/microstructure-estimator-report.md` | Auto-generated report (output) |
| `docs/feat/microstructure-estimator-report.json` | JSON summary for scripting |
| `docs/feat/microstructure-estimator-runbook.md` | This file |
| `scripts/extract_features_v2.sql` | Stage 1 feature extraction (SQL) |
| `scripts/microstructure_pipeline.sh` | End-to-end orchestrator (cron target) |
| `scripts/install_microstructure_cron.sh` | Cron installer |
| `polyagent/services/quant/ml/features.py` | VPIN + BNS jump primitives |
| `polyagent/services/quant/ml/extract.py` | Stage 1+2 orchestration |
| `polyagent/services/quant/ml/train.py` | Walk-forward + xgboost grid |
| `polyagent/services/quant/ml/report.py` | Markdown rendering |
| `polyagent/services/quant/ml/pipeline.py` | Python entrypoint |
| `tests/unit/quant_ml/*` | Unit + smoke tests |
| `microstructure_cron.log` | Per-tick log (gate checks, skips, runs) |
| `microstructure_pipeline.log` | Per-actual-run detailed log |

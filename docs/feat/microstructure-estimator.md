# Microstructure Estimator (xgboost) — Pre-Registration

**Date:** 2026-05-15
**Status:** PRE-REGISTERED (locked before fitting)
**Author:** Charles Borden
**Subsystem:** `quant_short` (BTC short-window up/down)

---

## Hypothesis

An xgboost classifier trained on point-in-time microstructure features plus operational controls will produce P(YES) predictions that **simultaneously**:

1. Beat constant-0.5 by at least 24 bp on OOS Brier (target: Brier ≤ 0.244)
2. Achieve OOS Murphy Resolution ≥ 0.008

Falsified if either threshold fails. Failing this, the `quant_short` subsystem is mothballed and capital reallocation is restricted to the main flow.

### Why these thresholds

- Constant-0.5 OOS Brier observed = 0.2500. The current lognormal Φ(d₂) estimator OOS Brier = 0.2998 (actively miscalibrated). A 24 bp improvement = entering the "informative but not deployable" zone.
- Prior microstructure scan (logistic regression, raw features) yielded OOS Resolution = 0.0015. 8× of that (0.008) is the minimum that supports a "this model carries real signal" claim.
- Both must hold: Resolution alone permits well-ordered but miscalibrated predictions; Brier alone can be improved by collapsing toward 0.5.

---

## Feature set (locked)

### Tier 1 — must-have, mechanism-supported

| Feature | Mechanism | Source |
|---|---|---|
| `bybit_aggr_flow_{10s,60s,300s,900s}` | Informed taker flow has persistent impact | `trade_prints` |
| `coinbase_aggr_flow_{10s,60s,300s,900s}` | Same on spot | `trade_prints` |
| `bybit_ofi_60s` (Σ ΔBidSize_L1 - ΔAskSize_L1) | OFI dominates static depth imbalance | `orderbook_snapshots` |
| `coinbase_ofi_60s` | Same on spot | `orderbook_snapshots` |
| `bybit_microprice_dev` ((bid·a + ask·b)/(a+b) - mid) | Microprice leads mid | `orderbook_snapshots` |
| `coinbase_microprice_dev` | Same on spot | `orderbook_snapshots` |
| `cross_venue_mid_diff_bps` (bybit_mid - coinbase_mid in bps) | Lead/lag between venues | `orderbook_snapshots` |
| `bybit_basis` | Perp basis, already materialized | `mark_index_prices` |

### Tier 2 — high-value extensions

| Feature | Mechanism | Source |
|---|---|---|
| `realized_vol_{1m,5m,15m}` (microprice-based) | Better than constant-σ; regime indicator | derived from `orderbook_snapshots.mid` |
| `jump_indicator_5m` (RV - BV, Barndorff-Nielsen/Shephard) | Discrete jumps mean-revert | derived |
| `ret_{10s,60s,300s}` (log returns) | Multi-horizon momentum / mean-reversion | derived |
| `vpin_50` (rolling toxicity, 50 volume buckets) | Informed/uninformed mix | derived from `trade_prints` |
| `spread_regime` (current spread / 1h median spread, each venue) | Stress indicator | derived |
| `strike_distance` (`(spot - strike) / spot`, signed) | Trivial but informative | from `quant_short_v` |

### Tier 3 — regime & operational controls

| Feature | Mechanism |
|---|---|
| `hour_sin`, `hour_cos` | Daily liquidity / vol cycle |
| `minutes_since_funding` | Funding flips at 8h boundaries |
| `time_into_window` ((decision_ts - window_start_ts) / window_duration_s) | Early vs late entry conditional |
| `window_5m` (1 if 5m, 0 if 15m) | Already significant in prior logit |
| `concurrent_with_prior` | Cluster control |

### Deliberately excluded

- **Funding rate as raw scalar.** Range too tight, numerical instability observed. Replace with binary indicator `|funding| > 95th percentile` only.
- **Polymarket implied prob.** Creates a feedback loop with `edge_at_decision`.
- **Hand-picked TA indicators (RSI, MACD).** No mechanism beyond what multi-horizon returns already capture.
- **vol_at_decision (current `vol_at_decision`).** Per `[[project-quant-high-edge-paused]]` we don't tune on this. Realized vol features replace it.

### Total feature count: 27

---

## Model class & search space

**Primary:** xgboost classifier.

**Hyperparameter grid (locked):**
- `max_depth` ∈ {3, 5, 7}
- `learning_rate` ∈ {0.01, 0.05, 0.10}
- `n_estimators`: determined by early stopping on validation fold
- Other params at xgboost defaults

**Total model variants (N for DSR):** 9.

**Baseline:** logistic regression on the same feature set, also 9 grid points (C ∈ {0.1, 1, 10} × penalty ∈ {l1, l2, elasticnet}). Treated as informational only; does not count toward N.

---

## Data requirements (pre-fit gate)

| Requirement | Reason |
|---|---|
| **Minimum 4000 joinable trades** | At ~27 features, a 60:1 row-to-feature ratio is the bare minimum for xgboost regularization to work. Current state: 1571 joinable; bot generates ~300/day; gate clears ~2026-05-23. |
| **At least 3 distinct trading days in OOS fold** | Regime coverage. |
| **No look-ahead.** Every feature computed strictly from `ts <= decision_ts`. | Look-ahead is the cardinal sin per the inference skill §5.1. |

---

## Validation strategy

**Walk-forward, 3 folds.** Time-ordered. No shuffling.

```
Fold 1: train on days 1..N-9, validate days N-8..N-6, test days N-5..N-3
Fold 2: train on days 1..N-6, validate days N-5..N-3, test days N-2..N
Fold 3: leave-future-out: train on full historical, test on next 3 days post-spec
```

- Hyperparameters selected on validation fold; test fold is **untouchable** until final report.
- Embargo: 24h between train and test in each fold (per López de Prado 2018 ch. 7) to prevent feature-label leakage on rolling-vol features.

---

## Multi-test correction

**DSR with N = 9** (xgboost grid). Reported alongside primary Brier / RES.

Selecting *which fold to report* is itself a multi-test problem. Mitigation: report **all three** folds, mean and worst.

---

## Decision rules

| Condition | Action |
|---|---|
| Both thresholds met on **all 3 folds** AND DSR ≥ 0.95 | DEPLOY at quarter-Kelly with 2-week paper-shadow first |
| Both thresholds met on **2 of 3 folds**, or DSR ∈ [0.50, 0.95) | MORE-RESEARCH; collect 2 more weeks; re-test against this same spec without modification |
| Fails on majority of folds OR DSR < 0.50 | ABANDON `quant_short` directional prediction; mothball subsystem |

---

## What constitutes "looking" at the data

Once this spec is committed (commit hash recorded below), the only legal operations are:
- Running the feature-extraction SQL (already shipped: `scripts/export_trades_with_microstructure.sql` + extensions for the new Tier 1/2/3 features)
- Running the walk-forward training script (to be added)
- Reading the final metric table

**Illegal:**
- Adding features after seeing intermediate results
- Changing thresholds after seeing intermediate results
- Re-running with different validation folds because the first ones looked bad
- Quietly dropping models from the 9-grid count

If any of these happen, the test is exploratory and DSR must be recomputed with the actual `N` of models considered.

---

## Companion canvas

`docs/feat/microstructure-estimator.canvas` visualises the feature flow, validation folds, and decision gates.

---

## Pre-commit hash

To be recorded at commit time: `<commit-hash>` (set after first commit of this doc).

# Microstructure Estimator — Training Report

**Generated:** 2026-06-01T14:29:32+00:00
**Spec commit (pre-registered):** `d5466937c6d17c893309d21668306c8f295459c8`
**N joinable trades:** 5360
**Decision:** **ABANDON**
**Rationale:** only 0/3 folds passed median Brier+RES thresholds

## Inference Report Block

```
Claim: xgboost on locked microstructure features beats the lognormal
       Phi(d2) baseline and exceeds Brier <= 0.244 AND RES >= 0.008 on all OOS folds.
Universe: BTC quant_short trades joined to bybit + coinbase market_data.
N joinable: 5360
Validation: 3-fold walk-forward, 24h embargo, time-ordered, no shuffle.
Multi-test: N=9 grid configs; median across configs reported per fold.
Mechanism: order-flow imbalance + cross-venue lead/lag + vol regime + funding cycle.
Pre-registration: docs/feat/microstructure-estimator.md @ d5466937c6d17c893309d21668306c8f295459c8
Decision: ABANDON   Rationale: only 0/3 folds passed median Brier+RES thresholds
```

## Per-fold metrics

| Fold | Train n | Test n | Train end | Test start | Median Brier | Median RES | Best Brier | Best config |
|---|---|---|---|---|---|---|---|---|
| 1 | 3216 | 964 | 2026-05-19 21:29:41.321456+00:00 | 2026-05-23 00:30:06.899280+00:00 | 0.2510 | 0.0007 | 0.2494 | d=5, lr=0.01 |
| 2 | 4020 | 804 | 2026-05-23 00:22:02.855123+00:00 | 2026-05-25 05:30:09.633027+00:00 | 0.2517 | 0.0005 | 0.2502 | d=3, lr=0.01 |
| 3 | 4288 | 536 | 2026-05-24 04:50:55.995389+00:00 | 2026-05-26 04:45:45.408946+00:00 | 0.2558 | 0.0018 | 0.2516 | d=3, lr=0.01 |

## Full grid x fold matrix

| Config | Fold 1 Brier | Fold 2 Brier | Fold 3 Brier | Fold 1 RES | Fold 2 RES | Fold 3 RES |
|---|---|---|---|---|---|---|
| d=3, lr=0.01 | 0.2495 | 0.2502 | 0.2516 | 0.0005 | 0.0000 | 0.0004 |
| d=3, lr=0.05 | 0.2505 | 0.2506 | 0.2542 | 0.0008 | 0.0004 | 0.0027 |
| d=3, lr=0.1 | 0.2510 | 0.2528 | 0.2577 | 0.0002 | 0.0011 | 0.0035 |
| d=5, lr=0.01 | 0.2494 | 0.2502 | 0.2519 | 0.0004 | 0.0000 | 0.0008 |
| d=5, lr=0.05 | 0.2510 | 0.2517 | 0.2558 | 0.0007 | 0.0011 | 0.0021 |
| d=5, lr=0.1 | 0.2533 | 0.2551 | 0.2593 | 0.0008 | 0.0007 | 0.0015 |
| d=7, lr=0.01 | 0.2497 | 0.2504 | 0.2529 | 0.0004 | 0.0000 | 0.0016 |
| d=7, lr=0.05 | 0.2510 | 0.2519 | 0.2566 | 0.0011 | 0.0005 | 0.0018 |
| d=7, lr=0.1 | 0.2549 | 0.2564 | 0.2642 | 0.0020 | 0.0007 | 0.0047 |

## Feature importance (gain) from best-Brier config per fold


**Fold 1 top features (gain):**

- `coinbase_aggr_flow_300s`: 7.694
- `hour_sin`: 7.655
- `ret_60s`: 7.244
- `jump_indicator_5m`: 7.223
- `realized_vol_5m`: 7.020
- `bybit_aggr_flow_900s`: 7.019
- `ret_10s`: 7.011
- `hour_cos`: 6.643
- `time_into_window`: 6.608
- `realized_vol_1m`: 6.553

**Fold 2 top features (gain):**

- `coinbase_aggr_flow_900s`: 9.350
- `coinbase_aggr_flow_300s`: 9.152
- `realized_vol_5m`: 8.972
- `ret_60s`: 8.534
- `hour_cos`: 8.472
- `jump_indicator_5m`: 8.447
- `coinbase_spread_regime`: 8.445
- `coinbase_microprice_dev`: 8.328
- `hour_sin`: 8.281
- `ret_300s`: 8.067

**Fold 3 top features (gain):**

- `coinbase_aggr_flow_300s`: 9.046
- `coinbase_aggr_flow_900s`: 8.201
- `hour_sin`: 8.074
- `vpin_50`: 7.912
- `coinbase_ofi_60s`: 7.843
- `jump_indicator_5m`: 7.839
- `bybit_aggr_flow_60s`: 7.796
- `ret_10s`: 7.591
- `bybit_aggr_flow_300s`: 7.485
- `bybit_aggr_flow_900s`: 7.460

## Locked feature list

These features were pre-registered. Any modification requires a fresh pre-registration with a new commit hash.

- `bybit_aggr_flow_10s`
- `bybit_aggr_flow_60s`
- `bybit_aggr_flow_300s`
- `bybit_aggr_flow_900s`
- `coinbase_aggr_flow_10s`
- `coinbase_aggr_flow_60s`
- `coinbase_aggr_flow_300s`
- `coinbase_aggr_flow_900s`
- `bybit_ofi_60s`
- `coinbase_ofi_60s`
- `bybit_microprice_dev`
- `coinbase_microprice_dev`
- `cross_venue_mid_diff_bps`
- `bybit_basis`
- `realized_vol_1m`
- `realized_vol_5m`
- `realized_vol_15m`
- `jump_indicator_5m`
- `ret_10s`
- `ret_60s`
- `ret_300s`
- `vpin_50`
- `bybit_spread_regime`
- `coinbase_spread_regime`
- `strike_distance`
- `hour_sin`
- `hour_cos`
- `minutes_since_funding`
- `time_into_window`
- `window_5m`
- `concurrent_with_prior`

## Thresholds

- Brier (median across 9 configs, per fold) must be ≤ **0.244**
- Resolution (median across 9 configs, per fold) must be ≥ **0.008**
- DEPLOY: both thresholds met on **all** folds
- MORE-RESEARCH: passed on n-1 of n folds
- ABANDON: passed on fewer than n-1 folds

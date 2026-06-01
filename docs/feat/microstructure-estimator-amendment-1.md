# Microstructure Estimator — Pre-Registration Amendment #1

**Date:** 2026-05-27
**Amends:** `docs/feat/microstructure-estimator.md` (original lock `d5466937`)
**Author:** Charles Borden
**Status:** PRE-REGISTERED (locked before fitting on the amended population)

---

## Why amend

The original pre-registration's population was "resolved `quant_short` *trades*", gated at 4000. That gate never cleared: the live paper bot exhausted its bankroll (~$1.36 free vs $50 start, -$48 realized) and stopped trading, freezing the joinable-trade count at ~1571.

Investigation (2026-05-27) found the data isn't scarce, it's **unlabeled**:

- `quant_decider_rejections` holds **8885 decision points with `p_up` computed** (the estimator ran), across **5546 distinct markets**, spanning 2026-05-06 to 2026-05-27. These accumulated independently of bankroll (they're evaluations, not trades).
- After dedup to one (latest) decision point per market, with a parseable slug and OB coverage: **5266 usable candidates** — well past the 4000 gate.
- They were unlabeled only because evaluated-but-not-traded markets were never persisted or resolved (1 of 5546 matched `quant_short_markets`).

## What changes

| Aspect | Original | Amended |
|---|---|---|
| **Population** | resolved trades | all evaluated decision points (traded + rejected-with-`p_up`), deduped to one latest point per market |
| **Label source** | resolver (Polymarket) | identical — Polymarket settled `winner`, fetched by conditionId. **Never spot-reconstructed** (resolver records ~43% spot/PM disagreement on these near-ATM windows) |
| **Expected N** | 4000 (never reached) | ~5266 resolvable, minus any not-yet-settled on PM |
| **Recovery mechanism** | n/a | parse window timing from slug; fetch outcome per conditionId; upsert into `quant_shadow_labels` |

## What stays LOCKED (unchanged from `d5466937`)

- **The 31-feature list** — identical. `LOCKED_FEATURES` in `train.py` is untouched.
- **The xgboost grid** — `max_depth ∈ {3,5,7} × lr ∈ {0.01,0.05,0.10}`, N=9.
- **Walk-forward validation** — 3 folds, 24h embargo, time-ordered.
- **Decision thresholds** — Brier ≤ 0.244 AND Resolution ≥ 0.008 on all folds; median-across-9 criterion; DEPLOY/MORE-RESEARCH/ABANDON rule.
- **Minimum N gate** — 4000 (now applied to the recovered labeled set).

Only the *population* and *how labels are obtained* change. The hypothesis, features, model, and success criteria are the same test.

## Selection-bias note (new, must be reported)

The rejected-with-`p_up` markets are the **low-|edge| slice** (|edge| below the trade threshold, or fees exceeded edge). Traded markets are the **high-|edge| slice**. Combining them gives fuller |edge| coverage, which is *favorable* for a direction predictor (edge magnitude shouldn't bias the feature→outcome relationship). Excluded entirely: markets rejected at earlier gates (`no_book`, `window_not_open`, `open_cap`, `bankroll_floor`) which never computed `p_up`. The report must state that the population is "markets that reached the edge-computation gate," not "all Polymarket short-horizon markets."

## Correlation note

Multiple evaluations of the same market are highly correlated (same outcome, near-identical features seconds apart). The recovery dedupes to **one decision point per market** (latest pre-close evaluation) to keep the effective sample honest. `concurrent_with_prior` remains a feature/control.

## Implementation artifacts

- `db/migrations/012_quant_shadow_labels.sql` — label store (also the forward shadow-logging target).
- `polyagent/services/quant/ml/recover.py` — backfill from rejections + Polymarket.
- `scripts/extract_features_shadow.sql` — feature extraction over `quant_shadow_labels` (VPIN + BNS jump computed in-SQL).
- `polyagent.services.quant.ml.pipeline --source shadow --recover-first` — runs recovery, extraction, training, report.

## Pre-commit hash

This amendment is locked at the commit that first introduces it (recorded after commit). Any further change to population, features, grid, or thresholds requires Amendment #2.

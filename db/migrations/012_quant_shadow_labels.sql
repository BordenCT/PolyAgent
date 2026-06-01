-- db/migrations/012_quant_shadow_labels.sql
-- Evaluated-decision-point label store for the microstructure estimator.
--
-- Motivation: the bot evaluates far more markets than it trades, and when
-- the paper bankroll is exhausted it evaluates but cannot trade at all.
-- Those evaluated-but-not-traded decision points are exactly the labeled
-- examples an ML predictor needs (features at decision_ts -> realized
-- outcome), and they accumulate independently of bankroll. They were
-- being logged to quant_decider_rejections but never persisted as
-- markets, so they were never resolved.
--
-- This table is both:
--   1. the backfill target for historical recovery (scripts/recover
--      reads rejections, reconstructs window timing from the slug, fetches
--      the authoritative outcome from Polymarket, and upserts here), and
--   2. the forward shadow-logging store (the live decider can upsert every
--      evaluated decision point here; a resolver pass fills `outcome`).
--
-- Outcome is ALWAYS Polymarket's settled winner, never spot-reconstructed:
-- the resolver docstring records ~43% disagreement between Coinbase-spot
-- derived outcomes and Polymarket settlement on these near-ATM short
-- windows, which would be catastrophic label noise.

CREATE TABLE IF NOT EXISTS quant_shadow_labels (
    id                 BIGSERIAL PRIMARY KEY,
    -- One row per market. conditionId is the natural key; a market is
    -- evaluated many times but we keep a single (latest) decision point
    -- to avoid intra-market correlation inflating the sample.
    polymarket_id      TEXT NOT NULL UNIQUE,
    slug               TEXT NOT NULL,
    asset_id           TEXT NOT NULL,

    -- Decision-time inputs (from the rejection row we recovered from, or
    -- from the live decider going forward).
    decision_ts        TIMESTAMPTZ NOT NULL,
    estimator_p_up     NUMERIC,
    spot_at_decision   NUMERIC,
    mid                NUMERIC,
    vol_at_decision    NUMERIC,
    abs_edge           NUMERIC,

    -- Window timing, parsed from the slug (<token>-updown-<dur>-<end_unix>).
    window_start_ts    TIMESTAMPTZ NOT NULL,
    window_end_ts      TIMESTAMPTZ NOT NULL,
    window_duration_s  INTEGER NOT NULL,

    -- Authoritative outcome from Polymarket. NULL until resolved/recovered.
    -- 'YES' = up token won, 'NO' = down token won.
    outcome            TEXT,
    -- Provenance: 'recovery' (historical backfill) or 'live' (forward log).
    source             TEXT NOT NULL DEFAULT 'recovery',
    recovered_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Time-range scans for feature extraction and walk-forward splits.
CREATE INDEX IF NOT EXISTS idx_quant_shadow_labels_decision_ts
    ON quant_shadow_labels(decision_ts);

-- Resolver pass: find unresolved rows whose window has closed.
CREATE INDEX IF NOT EXISTS idx_quant_shadow_labels_unresolved
    ON quant_shadow_labels(window_end_ts)
    WHERE outcome IS NULL;

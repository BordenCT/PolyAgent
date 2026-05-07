-- db/migrations/009_quant_short_observability.sql
-- Observability columns + rejections table for the short-horizon subsystem.
--
-- Adds visibility into:
--   * the much larger pool of considered-but-rejected markets
--     (quant_decider_rejections), so we can see what the bot is filtering
--     and why, not just what it traded;
--   * predicted vs realized economics (predicted_ev) per trade, so we can
--     tell whether the edge model translates into money;
--   * the spot-trajectory regime at decision time (return_5m/15m/30m and
--     realized_vol_5m), so we can ask "are losses concentrated after
--     fast moves?" without joining external data;
--   * resolution latency (resolution_lag_s), useful when investigating
--     stuck rows and tuning the resolver pass cadence;
--   * concurrent-trade clustering (concurrent_with_prior), since two
--     trades on overlapping windows are themselves correlated and shouldn't
--     be treated as independent samples in calibration analysis.
--
-- Existing rows on quant_short_trades remain valid; new columns default
-- to NULL (or FALSE for the boolean) so historical analysis is unaffected.

CREATE TABLE IF NOT EXISTS quant_decider_rejections (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_ts     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    polymarket_id   TEXT,
    slug            TEXT,
    asset_id        TEXT,
    reason          TEXT NOT NULL,
    -- Optional diagnostic fields. Captured when the rejecting gate has
    -- already computed them; left NULL when the gate fired earlier.
    abs_edge        NUMERIC,
    p_up            NUMERIC,
    mid             NUMERIC,
    fill_price      NUMERIC,
    vol             NUMERIC,
    spot            NUMERIC,
    -- Free-form fields the gate wanted to record (limits, headroom, etc.).
    -- JSONB to preserve key/value pairs without constraining schema.
    extra           JSONB
);

CREATE INDEX IF NOT EXISTS idx_quant_decider_rejections_ts
    ON quant_decider_rejections(decision_ts DESC);

CREATE INDEX IF NOT EXISTS idx_quant_decider_rejections_reason
    ON quant_decider_rejections(reason);


ALTER TABLE quant_short_trades
    ADD COLUMN IF NOT EXISTS predicted_ev NUMERIC;
-- Spot-trajectory snapshot captured at decision time. Returns are simple
-- (spot_now / spot_t - 1), unitless. realized_vol_5m is the same number
-- the decider already feeds into the estimator, persisted for analysis.
ALTER TABLE quant_short_trades
    ADD COLUMN IF NOT EXISTS return_5m NUMERIC;
ALTER TABLE quant_short_trades
    ADD COLUMN IF NOT EXISTS return_15m NUMERIC;
ALTER TABLE quant_short_trades
    ADD COLUMN IF NOT EXISTS return_30m NUMERIC;
ALTER TABLE quant_short_trades
    ADD COLUMN IF NOT EXISTS realized_vol_5m NUMERIC;
-- Filled by the resolver. NULL while pending; non-NULL after resolve.
ALTER TABLE quant_short_trades
    ADD COLUMN IF NOT EXISTS resolution_lag_s INTEGER;
-- TRUE when a same-asset trade was placed in the prior 60 seconds.
ALTER TABLE quant_short_trades
    ADD COLUMN IF NOT EXISTS concurrent_with_prior BOOLEAN NOT NULL DEFAULT FALSE;


-- View rebuild is performed in migration 010. CREATE OR REPLACE VIEW
-- requires that new columns be appended at the end of the projection;
-- the original draft of this migration interleaved analytics columns
-- with the existing market-side projection, which Postgres rejects as
-- a column rename ("cannot change name of view column ..."). The 010
-- file expresses the corrected ordering. Keeping it in a separate
-- migration leaves both 009's history and 010's fix re-runnable.

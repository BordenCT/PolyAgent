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


-- Rebuild the read view to expose the new columns. View definition is
-- otherwise unchanged.
CREATE OR REPLACE VIEW quant_short_v AS
SELECT
    -- trade identity and decision-time inputs
    t.id                       AS trade_id,
    t.market_id,
    t.decision_ts,
    t.side,
    t.fill_price_assumed,
    t.size,
    t.estimator_p_up,
    t.spot_at_decision,
    t.vol_at_decision,
    t.edge_at_decision,
    ABS(t.edge_at_decision)    AS abs_edge,
    t.pnl,
    t.resolved_at              AS trade_resolved_at,
    -- analytics columns added in 009
    t.predicted_ev,
    t.return_5m,
    t.return_15m,
    t.return_30m,
    t.realized_vol_5m,
    t.resolution_lag_s,
    t.concurrent_with_prior,

    -- market identity, window, outcome
    m.polymarket_id,
    m.slug,
    m.token_id_yes,
    m.token_id_no,
    m.window_duration_s,
    (m.window_duration_s / 60) AS window_minutes,
    m.window_start_ts,
    m.window_end_ts,
    m.start_spot,
    m.end_spot,
    m.outcome,
    m.asset_id,
    m.discovered_at,
    m.resolved_at              AS market_resolved_at,
    m.price_source_id,

    -- derived flags. NULL when the trade is unresolved (pnl IS NULL).
    -- NOTE: ties (pnl = 0) count as not-won, matching the CLI's
    -- pre-existing wins=`pnl > 0` / losses=`pnl <= 0` semantics.
    CASE
        WHEN t.pnl IS NULL THEN NULL
        WHEN t.pnl > 0     THEN TRUE
        ELSE FALSE
    END                        AS won,
    -- Brier score against the YES outcome. NULL until resolved.
    -- estimator_p_up is the bot's predicted P(YES); outcome=YES is 1, NO is 0.
    -- Lower is better. 0.25 = random, 0.0 = perfect.
    CASE
        WHEN m.outcome IS NULL THEN NULL
        WHEN m.outcome = 'YES' THEN POWER(t.estimator_p_up - 1, 2)
        WHEN m.outcome = 'NO'  THEN POWER(t.estimator_p_up,     2)
        ELSE NULL
    END                        AS brier
FROM quant_short_trades t
JOIN quant_short_markets m ON m.id = t.market_id;

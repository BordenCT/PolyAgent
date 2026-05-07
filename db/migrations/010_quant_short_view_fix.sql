-- db/migrations/010_quant_short_view_fix.sql
-- Re-create quant_short_v with the migration-009 analytics columns and
-- the derived `brier` field appended at the END of the column list.
--
-- Why: PostgreSQL's CREATE OR REPLACE VIEW requires that new column N
-- match the existing column N by name. Migration 009 inserted new
-- columns in the middle of the projection, which made PG try to rename
-- existing columns (e.g. polymarket_id -> predicted_ev) and fail with:
--
--   ERROR: cannot change name of view column "polymarket_id" to "predicted_ev"
--
-- Adding only-at-the-tail columns sidesteps that: every existing
-- consumer's column references remain valid, and the new analytics
-- fields just get appended.
--
-- Note: ``polyagent_trades_v`` (migration 008) depends on quant_short_v
-- but only uses columns that already existed in 007; it does not need
-- to be rebuilt by this migration.

CREATE OR REPLACE VIEW quant_short_v AS
SELECT
    -- trade identity and decision-time inputs (original 007 ordering)
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

    -- market identity, window, outcome (original 007 ordering)
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

    -- derived flag (original 007 ordering). NULL when unresolved.
    -- Ties (pnl = 0) count as not-won, matching the CLI's
    -- pre-existing wins=`pnl > 0` / losses=`pnl <= 0` semantics.
    CASE
        WHEN t.pnl IS NULL THEN NULL
        WHEN t.pnl > 0     THEN TRUE
        ELSE FALSE
    END                        AS won,

    -- ANALYTICS COLUMNS APPENDED IN 009/010. Anything new must be added
    -- after this line to keep CREATE OR REPLACE happy on future rebuilds.
    t.predicted_ev,
    t.return_5m,
    t.return_15m,
    t.return_30m,
    t.realized_vol_5m,
    t.resolution_lag_s,
    t.concurrent_with_prior,
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

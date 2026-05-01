-- db/migrations/007_quant_short_view.sql
-- Denormalized read view over quant_short_trades JOIN quant_short_markets.
--
-- Lets analytics CLIs (quant_stats, performance) and ad-hoc SQL query a
-- single flat surface instead of repeating the join, and surfaces commonly
-- derived fields (|edge|, won, window_minutes) once. New analytics columns
-- can be added here without altering either underlying table.

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
    END                        AS won
FROM quant_short_trades t
JOIN quant_short_markets m ON m.id = t.market_id;

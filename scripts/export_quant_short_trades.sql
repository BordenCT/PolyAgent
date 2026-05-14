-- Resolved quant_short trades for offline inference work.
-- See scripts/export_quant_short_trades.sh for invocation.

SELECT
    trade_id,
    market_id,
    asset_id,
    decision_ts,
    side,
    estimator_p_up,
    edge_at_decision,
    abs_edge,
    fill_price_assumed,
    size,
    vol_at_decision,
    window_duration_s,
    outcome,
    pnl,
    won,
    brier,
    concurrent_with_prior,
    trade_resolved_at,
    market_resolved_at,
    start_spot,
    end_spot,
    slug
FROM quant_short_v
WHERE pnl IS NOT NULL
ORDER BY decision_ts;

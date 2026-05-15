-- Join each resolved quant_short trade to the nearest market_data
-- snapshot per (venue, product), plus a 60s rolling aggressor-flow
-- aggregate. Produces a wide feature matrix for offline modeling.
--
-- Lateral joins with `ORDER BY ts DESC LIMIT 1` and the (venue,
-- product, ts DESC) btree indexes give us O(log n) per lookup;
-- 2549 trades * 6 lookups = a few thousand index probes total.
--
-- LEFT JOINs: trades before 2026-05-07 ~18:27 predate market_data and
-- will have NULL feature columns. Filter those out client-side.

SELECT
    -- Original trade fields (subset of quant_short_v columns we need).
    t.trade_id,
    t.market_id,
    t.decision_ts,
    t.side,
    t.estimator_p_up,
    t.edge_at_decision,
    t.abs_edge,
    t.fill_price_assumed,
    t.size,
    t.vol_at_decision,
    t.window_duration_s,
    t.outcome,
    t.pnl,
    t.won,
    t.brier,
    t.concurrent_with_prior,

    -- Bybit perp orderbook at decision_ts (most recent <= decision_ts).
    ob_b.imbalance        AS bybit_ob_imb,
    ob_b.spread           AS bybit_ob_spread,
    ob_b.mid              AS bybit_ob_mid,
    ob_b.bid_volume_topN  AS bybit_bid_vol,
    ob_b.ask_volume_topN  AS bybit_ask_vol,

    -- Coinbase spot orderbook at decision_ts.
    ob_c.imbalance        AS coinbase_ob_imb,
    ob_c.spread           AS coinbase_ob_spread,
    ob_c.mid              AS coinbase_ob_mid,
    ob_c.bid_volume_topN  AS coinbase_bid_vol,
    ob_c.ask_volume_topN  AS coinbase_ask_vol,

    -- Bybit perp mark/index/basis.
    mi.mark_price         AS bybit_mark,
    mi.index_price        AS bybit_index,
    mi.basis              AS bybit_basis,

    -- Bybit funding rate at decision_ts.
    fh.funding_rate       AS bybit_funding,
    fh.annualised_rate    AS bybit_funding_ann,

    -- 60s aggressor flow (bybit perp). Bounded ratio [-1, 1].
    tf_b.n_trades_60s     AS bybit_n_trades_60s,
    tf_b.volume_60s       AS bybit_volume_60s,
    CASE WHEN COALESCE(tf_b.volume_60s, 0) > 0
         THEN tf_b.signed_flow_60s / tf_b.volume_60s
         ELSE NULL
    END                   AS bybit_aggr_flow_60s,

    -- 60s aggressor flow (coinbase spot).
    tf_c.n_trades_60s     AS coinbase_n_trades_60s,
    tf_c.volume_60s       AS coinbase_volume_60s,
    CASE WHEN COALESCE(tf_c.volume_60s, 0) > 0
         THEN tf_c.signed_flow_60s / tf_c.volume_60s
         ELSE NULL
    END                   AS coinbase_aggr_flow_60s

FROM quant_short_v t
LEFT JOIN LATERAL (
    SELECT imbalance, spread, mid, bid_volume_topN, ask_volume_topN
    FROM orderbook_snapshots
    WHERE venue = 'bybit' AND product = 'BTCUSDT'
      AND ts <= t.decision_ts
    ORDER BY ts DESC
    LIMIT 1
) ob_b ON TRUE
LEFT JOIN LATERAL (
    SELECT imbalance, spread, mid, bid_volume_topN, ask_volume_topN
    FROM orderbook_snapshots
    WHERE venue = 'coinbase' AND product = 'BTC-USD'
      AND ts <= t.decision_ts
    ORDER BY ts DESC
    LIMIT 1
) ob_c ON TRUE
LEFT JOIN LATERAL (
    SELECT mark_price, index_price, basis
    FROM mark_index_prices
    WHERE venue = 'bybit' AND product = 'BTCUSDT'
      AND ts <= t.decision_ts
    ORDER BY ts DESC
    LIMIT 1
) mi ON TRUE
LEFT JOIN LATERAL (
    SELECT funding_rate, annualised_rate
    FROM funding_history
    WHERE venue = 'bybit' AND product = 'BTCUSDT'
      AND ts <= t.decision_ts
    ORDER BY ts DESC
    LIMIT 1
) fh ON TRUE
LEFT JOIN LATERAL (
    SELECT
        COUNT(*)                                            AS n_trades_60s,
        COALESCE(SUM(size), 0)                              AS volume_60s,
        COALESCE(SUM(CASE WHEN side IN ('buy', 'Buy')
                          THEN size ELSE -size END), 0)     AS signed_flow_60s
    FROM trade_prints
    WHERE venue = 'bybit' AND product = 'BTCUSDT'
      AND ts BETWEEN t.decision_ts - INTERVAL '60 seconds'
                 AND t.decision_ts
) tf_b ON TRUE
LEFT JOIN LATERAL (
    SELECT
        COUNT(*)                                            AS n_trades_60s,
        COALESCE(SUM(size), 0)                              AS volume_60s,
        COALESCE(SUM(CASE WHEN side IN ('buy', 'Buy')
                          THEN size ELSE -size END), 0)     AS signed_flow_60s
    FROM trade_prints
    WHERE venue = 'coinbase' AND product = 'BTC-USD'
      AND ts BETWEEN t.decision_ts - INTERVAL '60 seconds'
                 AND t.decision_ts
) tf_c ON TRUE
WHERE t.pnl IS NOT NULL
ORDER BY t.decision_ts;

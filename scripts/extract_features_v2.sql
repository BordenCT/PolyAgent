-- Stage 1 feature extraction for the microstructure-estimator
-- pre-registration (docs/feat/microstructure-estimator.md, locked at
-- commit d5466937). Produces a wide CSV with all SQL-friendly features
-- per resolved quant_short trade. Tier 2 features that need a returns
-- array (VPIN, jump indicator) are computed in Python from a second
-- lighter-weight pull.
--
-- This SQL is referenced by docs/feat/microstructure-estimator.md;
-- changing it requires a new pre-registration doc.

SELECT
    -- ============================================================
    -- Trade identity & target
    -- ============================================================
    t.trade_id,
    t.market_id,
    t.decision_ts,
    t.side,
    t.outcome,
    t.pnl,
    t.won,
    t.brier,
    t.estimator_p_up,                            -- baseline model output
    t.fill_price_assumed,
    t.size,
    t.window_duration_s,
    m.window_start_ts,
    m.window_end_ts,
    m.start_spot,
    t.spot_at_decision,

    -- ============================================================
    -- TIER 1: must-have microstructure (mechanism-supported)
    -- ============================================================

    -- Multi-horizon aggressor flow (bybit perp). Signed flow / volume.
    tf_b_10s.flow_ratio                          AS bybit_aggr_flow_10s,
    tf_b_60s.flow_ratio                          AS bybit_aggr_flow_60s,
    tf_b_300s.flow_ratio                         AS bybit_aggr_flow_300s,
    tf_b_900s.flow_ratio                         AS bybit_aggr_flow_900s,
    tf_b_60s.n_trades                            AS bybit_n_trades_60s,
    tf_b_60s.volume                              AS bybit_volume_60s,

    -- Multi-horizon aggressor flow (coinbase spot).
    tf_c_10s.flow_ratio                          AS coinbase_aggr_flow_10s,
    tf_c_60s.flow_ratio                          AS coinbase_aggr_flow_60s,
    tf_c_300s.flow_ratio                         AS coinbase_aggr_flow_300s,
    tf_c_900s.flow_ratio                         AS coinbase_aggr_flow_900s,
    tf_c_60s.n_trades                            AS coinbase_n_trades_60s,
    tf_c_60s.volume                              AS coinbase_volume_60s,

    -- OFI 60s (Order Flow Imbalance from depth changes). Computed by
    -- diffing top-of-book size over the window. Sign convention:
    -- positive = net bid pressure.
    ofi_b.ofi_60s                                AS bybit_ofi_60s,
    ofi_c.ofi_60s                                AS coinbase_ofi_60s,

    -- Microprice deviation from mid. Microprice =
    -- (best_bid * ask_size + best_ask * bid_size) / (bid + ask).
    -- For top-N, we approximate using L1 prices weighted by topN volume.
    CASE WHEN ob_b.bid_volume_topN + ob_b.ask_volume_topN > 0
         THEN ((ob_b.best_ask * ob_b.bid_volume_topN
              + ob_b.best_bid * ob_b.ask_volume_topN)
              / (ob_b.bid_volume_topN + ob_b.ask_volume_topN)) - ob_b.mid
         ELSE 0
    END                                          AS bybit_microprice_dev,
    CASE WHEN ob_c.bid_volume_topN + ob_c.ask_volume_topN > 0
         THEN ((ob_c.best_ask * ob_c.bid_volume_topN
              + ob_c.best_bid * ob_c.ask_volume_topN)
              / (ob_c.bid_volume_topN + ob_c.ask_volume_topN)) - ob_c.mid
         ELSE 0
    END                                          AS coinbase_microprice_dev,

    -- Cross-venue mid diff in bps (positive = bybit perp > coinbase spot).
    CASE WHEN ob_c.mid > 0
         THEN 10000 * (ob_b.mid - ob_c.mid) / ob_c.mid
         ELSE NULL
    END                                          AS cross_venue_mid_diff_bps,

    -- Bybit perp basis (mark - index). Already materialized.
    mi.basis                                     AS bybit_basis,

    -- Point-in-time OB scalars (used for spread features in Tier 2 too).
    ob_b.imbalance                               AS bybit_ob_imb,
    ob_b.spread                                  AS bybit_ob_spread,
    ob_b.mid                                     AS bybit_ob_mid,
    ob_c.imbalance                               AS coinbase_ob_imb,
    ob_c.spread                                  AS coinbase_ob_spread,
    ob_c.mid                                     AS coinbase_ob_mid,

    -- ============================================================
    -- TIER 2: high-value extensions
    -- ============================================================

    -- Realized vol from coinbase spot mid, log returns.
    -- Computed as STDDEV(log(mid_t / mid_{t-1})) over a windowed pull.
    rv_c_60.rv                                   AS realized_vol_1m,
    rv_c_300.rv                                  AS realized_vol_5m,
    rv_c_900.rv                                  AS realized_vol_15m,

    -- Log returns at decision_ts vs N seconds earlier (coinbase spot).
    CASE WHEN ob_c.mid > 0 AND ret_c_10.mid_then > 0
         THEN LN(ob_c.mid / ret_c_10.mid_then) ELSE NULL
    END                                          AS ret_10s,
    CASE WHEN ob_c.mid > 0 AND ret_c_60.mid_then > 0
         THEN LN(ob_c.mid / ret_c_60.mid_then) ELSE NULL
    END                                          AS ret_60s,
    CASE WHEN ob_c.mid > 0 AND ret_c_300.mid_then > 0
         THEN LN(ob_c.mid / ret_c_300.mid_then) ELSE NULL
    END                                          AS ret_300s,

    -- Spread regime: current spread / rolling 1h median spread per venue.
    -- Median in SQL via percentile_cont in LATERAL.
    CASE WHEN sr_b.median_spread > 0
         THEN ob_b.spread / sr_b.median_spread ELSE NULL
    END                                          AS bybit_spread_regime,
    CASE WHEN sr_c.median_spread > 0
         THEN ob_c.spread / sr_c.median_spread ELSE NULL
    END                                          AS coinbase_spread_regime,

    -- Strike distance: (spot_at_decision - start_spot) / start_spot.
    -- Signed. start_spot is the strike for up/down binaries.
    CASE WHEN m.start_spot > 0
         THEN (t.spot_at_decision - m.start_spot) / m.start_spot
         ELSE NULL
    END                                          AS strike_distance,

    -- ============================================================
    -- TIER 3: regime & operational controls
    -- ============================================================

    -- Hour-of-day, cyclical (sin/cos of UTC hour).
    SIN(2 * PI() * EXTRACT(HOUR FROM t.decision_ts AT TIME ZONE 'UTC') / 24.0)
                                                 AS hour_sin,
    COS(2 * PI() * EXTRACT(HOUR FROM t.decision_ts AT TIME ZONE 'UTC') / 24.0)
                                                 AS hour_cos,

    -- Minutes since last funding settlement. Bybit funds every 8h; we
    -- find the most-recent settlement <= decision_ts.
    EXTRACT(EPOCH FROM (t.decision_ts - fh.ts)) / 60.0
                                                 AS minutes_since_funding,

    -- Time into window (0 = window just opened, 1 = about to close).
    CASE WHEN m.window_duration_s > 0
         THEN EXTRACT(EPOCH FROM (t.decision_ts - m.window_start_ts))
              / m.window_duration_s
         ELSE NULL
    END                                          AS time_into_window,

    -- 5m vs 15m bet flag.
    CASE WHEN m.window_duration_s = 300 THEN 1 ELSE 0 END
                                                 AS window_5m,

    -- Cluster control.
    CASE WHEN t.concurrent_with_prior THEN 1 ELSE 0 END
                                                 AS concurrent_with_prior

FROM quant_short_trades t
JOIN quant_short_markets m ON m.id = t.market_id

-- Bybit OB at decision_ts
LEFT JOIN LATERAL (
    SELECT imbalance, spread, mid, best_bid, best_ask, bid_volume_topN, ask_volume_topN
    FROM orderbook_snapshots
    WHERE venue='bybit' AND product='BTCUSDT' AND ts <= t.decision_ts
    ORDER BY ts DESC LIMIT 1
) ob_b ON TRUE

-- Coinbase OB at decision_ts
LEFT JOIN LATERAL (
    SELECT imbalance, spread, mid, best_bid, best_ask, bid_volume_topN, ask_volume_topN
    FROM orderbook_snapshots
    WHERE venue='coinbase' AND product='BTC-USD' AND ts <= t.decision_ts
    ORDER BY ts DESC LIMIT 1
) ob_c ON TRUE

-- Bybit mark/basis at decision_ts
LEFT JOIN LATERAL (
    SELECT basis, mark_price, index_price
    FROM mark_index_prices
    WHERE venue='bybit' AND product='BTCUSDT' AND ts <= t.decision_ts
    ORDER BY ts DESC LIMIT 1
) mi ON TRUE

-- Bybit funding row at decision_ts
LEFT JOIN LATERAL (
    SELECT ts, funding_rate
    FROM funding_history
    WHERE venue='bybit' AND product='BTCUSDT' AND ts <= t.decision_ts
    ORDER BY ts DESC LIMIT 1
) fh ON TRUE

-- Bybit aggressor flow, multiple horizons. Macro pattern repeated.
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS n_trades,
           COALESCE(SUM(size), 0) AS volume,
           CASE WHEN COALESCE(SUM(size), 0) > 0
                THEN COALESCE(SUM(CASE WHEN side IN ('buy','Buy') THEN size ELSE -size END), 0) / SUM(size)
                ELSE NULL
           END AS flow_ratio
    FROM trade_prints
    WHERE venue='bybit' AND product='BTCUSDT'
      AND ts BETWEEN t.decision_ts - INTERVAL '10 seconds' AND t.decision_ts
) tf_b_10s ON TRUE
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS n_trades,
           COALESCE(SUM(size), 0) AS volume,
           CASE WHEN COALESCE(SUM(size), 0) > 0
                THEN COALESCE(SUM(CASE WHEN side IN ('buy','Buy') THEN size ELSE -size END), 0) / SUM(size)
                ELSE NULL
           END AS flow_ratio
    FROM trade_prints
    WHERE venue='bybit' AND product='BTCUSDT'
      AND ts BETWEEN t.decision_ts - INTERVAL '60 seconds' AND t.decision_ts
) tf_b_60s ON TRUE
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS n_trades,
           COALESCE(SUM(size), 0) AS volume,
           CASE WHEN COALESCE(SUM(size), 0) > 0
                THEN COALESCE(SUM(CASE WHEN side IN ('buy','Buy') THEN size ELSE -size END), 0) / SUM(size)
                ELSE NULL
           END AS flow_ratio
    FROM trade_prints
    WHERE venue='bybit' AND product='BTCUSDT'
      AND ts BETWEEN t.decision_ts - INTERVAL '300 seconds' AND t.decision_ts
) tf_b_300s ON TRUE
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS n_trades,
           COALESCE(SUM(size), 0) AS volume,
           CASE WHEN COALESCE(SUM(size), 0) > 0
                THEN COALESCE(SUM(CASE WHEN side IN ('buy','Buy') THEN size ELSE -size END), 0) / SUM(size)
                ELSE NULL
           END AS flow_ratio
    FROM trade_prints
    WHERE venue='bybit' AND product='BTCUSDT'
      AND ts BETWEEN t.decision_ts - INTERVAL '900 seconds' AND t.decision_ts
) tf_b_900s ON TRUE

-- Coinbase aggressor flow, multiple horizons.
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS n_trades,
           COALESCE(SUM(size), 0) AS volume,
           CASE WHEN COALESCE(SUM(size), 0) > 0
                THEN COALESCE(SUM(CASE WHEN side IN ('buy','Buy') THEN size ELSE -size END), 0) / SUM(size)
                ELSE NULL
           END AS flow_ratio
    FROM trade_prints
    WHERE venue='coinbase' AND product='BTC-USD'
      AND ts BETWEEN t.decision_ts - INTERVAL '10 seconds' AND t.decision_ts
) tf_c_10s ON TRUE
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS n_trades,
           COALESCE(SUM(size), 0) AS volume,
           CASE WHEN COALESCE(SUM(size), 0) > 0
                THEN COALESCE(SUM(CASE WHEN side IN ('buy','Buy') THEN size ELSE -size END), 0) / SUM(size)
                ELSE NULL
           END AS flow_ratio
    FROM trade_prints
    WHERE venue='coinbase' AND product='BTC-USD'
      AND ts BETWEEN t.decision_ts - INTERVAL '60 seconds' AND t.decision_ts
) tf_c_60s ON TRUE
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS n_trades,
           COALESCE(SUM(size), 0) AS volume,
           CASE WHEN COALESCE(SUM(size), 0) > 0
                THEN COALESCE(SUM(CASE WHEN side IN ('buy','Buy') THEN size ELSE -size END), 0) / SUM(size)
                ELSE NULL
           END AS flow_ratio
    FROM trade_prints
    WHERE venue='coinbase' AND product='BTC-USD'
      AND ts BETWEEN t.decision_ts - INTERVAL '300 seconds' AND t.decision_ts
) tf_c_300s ON TRUE
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS n_trades,
           COALESCE(SUM(size), 0) AS volume,
           CASE WHEN COALESCE(SUM(size), 0) > 0
                THEN COALESCE(SUM(CASE WHEN side IN ('buy','Buy') THEN size ELSE -size END), 0) / SUM(size)
                ELSE NULL
           END AS flow_ratio
    FROM trade_prints
    WHERE venue='coinbase' AND product='BTC-USD'
      AND ts BETWEEN t.decision_ts - INTERVAL '900 seconds' AND t.decision_ts
) tf_c_900s ON TRUE

-- OFI 60s: net change in top-of-book size. Use first and last L1 sizes
-- over the window and diff. Sign: (bid_end - bid_start) - (ask_end - ask_start).
LEFT JOIN LATERAL (
    SELECT
      (SELECT bid_volume_topN FROM orderbook_snapshots
         WHERE venue='bybit' AND product='BTCUSDT'
           AND ts BETWEEN t.decision_ts - INTERVAL '60 seconds' AND t.decision_ts
         ORDER BY ts DESC LIMIT 1) -
      (SELECT bid_volume_topN FROM orderbook_snapshots
         WHERE venue='bybit' AND product='BTCUSDT'
           AND ts BETWEEN t.decision_ts - INTERVAL '60 seconds' AND t.decision_ts
         ORDER BY ts ASC LIMIT 1) -
      ((SELECT ask_volume_topN FROM orderbook_snapshots
         WHERE venue='bybit' AND product='BTCUSDT'
           AND ts BETWEEN t.decision_ts - INTERVAL '60 seconds' AND t.decision_ts
         ORDER BY ts DESC LIMIT 1) -
       (SELECT ask_volume_topN FROM orderbook_snapshots
         WHERE venue='bybit' AND product='BTCUSDT'
           AND ts BETWEEN t.decision_ts - INTERVAL '60 seconds' AND t.decision_ts
         ORDER BY ts ASC LIMIT 1)) AS ofi_60s
) ofi_b ON TRUE
LEFT JOIN LATERAL (
    SELECT
      (SELECT bid_volume_topN FROM orderbook_snapshots
         WHERE venue='coinbase' AND product='BTC-USD'
           AND ts BETWEEN t.decision_ts - INTERVAL '60 seconds' AND t.decision_ts
         ORDER BY ts DESC LIMIT 1) -
      (SELECT bid_volume_topN FROM orderbook_snapshots
         WHERE venue='coinbase' AND product='BTC-USD'
           AND ts BETWEEN t.decision_ts - INTERVAL '60 seconds' AND t.decision_ts
         ORDER BY ts ASC LIMIT 1) -
      ((SELECT ask_volume_topN FROM orderbook_snapshots
         WHERE venue='coinbase' AND product='BTC-USD'
           AND ts BETWEEN t.decision_ts - INTERVAL '60 seconds' AND t.decision_ts
         ORDER BY ts DESC LIMIT 1) -
       (SELECT ask_volume_topN FROM orderbook_snapshots
         WHERE venue='coinbase' AND product='BTC-USD'
           AND ts BETWEEN t.decision_ts - INTERVAL '60 seconds' AND t.decision_ts
         ORDER BY ts ASC LIMIT 1)) AS ofi_60s
) ofi_c ON TRUE

-- Realized vol from coinbase spot mid, multiple horizons.
LEFT JOIN LATERAL (
    SELECT STDDEV(LN(mid / NULLIF(prev_mid, 0))) AS rv
    FROM (
      SELECT mid,
             LAG(mid) OVER (ORDER BY ts) AS prev_mid
      FROM orderbook_snapshots
      WHERE venue='coinbase' AND product='BTC-USD'
        AND ts BETWEEN t.decision_ts - INTERVAL '60 seconds' AND t.decision_ts
        AND mid > 0
    ) x
    WHERE prev_mid IS NOT NULL
) rv_c_60 ON TRUE
LEFT JOIN LATERAL (
    SELECT STDDEV(LN(mid / NULLIF(prev_mid, 0))) AS rv
    FROM (
      SELECT mid,
             LAG(mid) OVER (ORDER BY ts) AS prev_mid
      FROM orderbook_snapshots
      WHERE venue='coinbase' AND product='BTC-USD'
        AND ts BETWEEN t.decision_ts - INTERVAL '300 seconds' AND t.decision_ts
        AND mid > 0
    ) x
    WHERE prev_mid IS NOT NULL
) rv_c_300 ON TRUE
LEFT JOIN LATERAL (
    SELECT STDDEV(LN(mid / NULLIF(prev_mid, 0))) AS rv
    FROM (
      SELECT mid,
             LAG(mid) OVER (ORDER BY ts) AS prev_mid
      FROM orderbook_snapshots
      WHERE venue='coinbase' AND product='BTC-USD'
        AND ts BETWEEN t.decision_ts - INTERVAL '900 seconds' AND t.decision_ts
        AND mid > 0
    ) x
    WHERE prev_mid IS NOT NULL
) rv_c_900 ON TRUE

-- Multi-horizon mids for return calculation (coinbase spot).
LEFT JOIN LATERAL (
    SELECT mid AS mid_then FROM orderbook_snapshots
    WHERE venue='coinbase' AND product='BTC-USD'
      AND ts <= t.decision_ts - INTERVAL '10 seconds'
    ORDER BY ts DESC LIMIT 1
) ret_c_10 ON TRUE
LEFT JOIN LATERAL (
    SELECT mid AS mid_then FROM orderbook_snapshots
    WHERE venue='coinbase' AND product='BTC-USD'
      AND ts <= t.decision_ts - INTERVAL '60 seconds'
    ORDER BY ts DESC LIMIT 1
) ret_c_60 ON TRUE
LEFT JOIN LATERAL (
    SELECT mid AS mid_then FROM orderbook_snapshots
    WHERE venue='coinbase' AND product='BTC-USD'
      AND ts <= t.decision_ts - INTERVAL '300 seconds'
    ORDER BY ts DESC LIMIT 1
) ret_c_300 ON TRUE

-- Spread-regime medians (1h rolling).
LEFT JOIN LATERAL (
    SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY spread) AS median_spread
    FROM orderbook_snapshots
    WHERE venue='bybit' AND product='BTCUSDT'
      AND ts BETWEEN t.decision_ts - INTERVAL '1 hour' AND t.decision_ts
) sr_b ON TRUE
LEFT JOIN LATERAL (
    SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY spread) AS median_spread
    FROM orderbook_snapshots
    WHERE venue='coinbase' AND product='BTC-USD'
      AND ts BETWEEN t.decision_ts - INTERVAL '1 hour' AND t.decision_ts
) sr_c ON TRUE

WHERE t.pnl IS NOT NULL
ORDER BY t.decision_ts;

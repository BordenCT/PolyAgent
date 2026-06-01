-- Feature extraction for recovered shadow labels.
-- Mirrors scripts/extract_features_v2.sql but sources decision points from
-- quant_shadow_labels (recovered evaluated markets) instead of trades.
-- Produces the locked 31-feature schema + outcome target.
--
-- Only resolved rows (outcome IS NOT NULL) are emitted. start_spot (the
-- up/down strike) is taken from the coinbase spot mid at window_start_ts.
--
-- Referenced by docs/feat/microstructure-estimator.md. Changing it
-- requires a pre-registration amendment.

SELECT
    s.polymarket_id                              AS trade_id,
    s.polymarket_id                              AS market_id,
    s.decision_ts,
    s.outcome,
    s.estimator_p_up,
    s.window_duration_s,
    s.window_start_ts,
    s.window_end_ts,
    s.spot_at_decision,
    ss.start_mid                                 AS start_spot,

    -- TIER 1
    tf_b_10s.flow_ratio                          AS bybit_aggr_flow_10s,
    tf_b_60s.flow_ratio                          AS bybit_aggr_flow_60s,
    tf_b_300s.flow_ratio                         AS bybit_aggr_flow_300s,
    tf_b_900s.flow_ratio                         AS bybit_aggr_flow_900s,
    tf_c_10s.flow_ratio                          AS coinbase_aggr_flow_10s,
    tf_c_60s.flow_ratio                          AS coinbase_aggr_flow_60s,
    tf_c_300s.flow_ratio                         AS coinbase_aggr_flow_300s,
    tf_c_900s.flow_ratio                         AS coinbase_aggr_flow_900s,
    ofi_b.ofi_60s                                AS bybit_ofi_60s,
    ofi_c.ofi_60s                                AS coinbase_ofi_60s,
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
    CASE WHEN ob_c.mid > 0
         THEN 10000 * (ob_b.mid - ob_c.mid) / ob_c.mid
         ELSE NULL
    END                                          AS cross_venue_mid_diff_bps,
    mi.basis                                     AS bybit_basis,

    -- TIER 2
    rv_c_60.rv                                   AS realized_vol_1m,
    rv_c_300.rv                                  AS realized_vol_5m,
    rv_c_900.rv                                  AS realized_vol_15m,
    CASE WHEN jump.rv > 0 THEN (jump.rv - jump.bv) / jump.rv ELSE 0 END
                                                 AS jump_indicator_5m,
    CASE WHEN ob_c.mid > 0 AND ret_c_10.mid_then > 0
         THEN LN(ob_c.mid / ret_c_10.mid_then) ELSE NULL
    END                                          AS ret_10s,
    CASE WHEN ob_c.mid > 0 AND ret_c_60.mid_then > 0
         THEN LN(ob_c.mid / ret_c_60.mid_then) ELSE NULL
    END                                          AS ret_60s,
    CASE WHEN ob_c.mid > 0 AND ret_c_300.mid_then > 0
         THEN LN(ob_c.mid / ret_c_300.mid_then) ELSE NULL
    END                                          AS ret_300s,
    vpin.vpin_50                                 AS vpin_50,
    CASE WHEN sr_b.median_spread > 0
         THEN ob_b.spread / sr_b.median_spread ELSE NULL
    END                                          AS bybit_spread_regime,
    CASE WHEN sr_c.median_spread > 0
         THEN ob_c.spread / sr_c.median_spread ELSE NULL
    END                                          AS coinbase_spread_regime,
    CASE WHEN ss.start_mid > 0
         THEN (s.spot_at_decision - ss.start_mid) / ss.start_mid
         ELSE NULL
    END                                          AS strike_distance,

    -- TIER 3
    SIN(2 * PI() * EXTRACT(HOUR FROM s.decision_ts AT TIME ZONE 'UTC') / 24.0)
                                                 AS hour_sin,
    COS(2 * PI() * EXTRACT(HOUR FROM s.decision_ts AT TIME ZONE 'UTC') / 24.0)
                                                 AS hour_cos,
    EXTRACT(EPOCH FROM (s.decision_ts - fh.ts)) / 60.0
                                                 AS minutes_since_funding,
    CASE WHEN s.window_duration_s > 0
         THEN EXTRACT(EPOCH FROM (s.decision_ts - s.window_start_ts))
              / s.window_duration_s
         ELSE NULL
    END                                          AS time_into_window,
    CASE WHEN s.window_duration_s = 300 THEN 1 ELSE 0 END
                                                 AS window_5m,
    -- Concurrency control: another evaluated decision point on this asset
    -- within the prior 60s. Computed against the shadow table itself.
    CASE WHEN EXISTS (
        SELECT 1 FROM quant_shadow_labels s2
        WHERE s2.asset_id = s.asset_id
          AND s2.polymarket_id <> s.polymarket_id
          AND s2.decision_ts BETWEEN s.decision_ts - INTERVAL '60 seconds'
                                 AND s.decision_ts
    ) THEN 1 ELSE 0 END                          AS concurrent_with_prior

FROM quant_shadow_labels s

-- start_spot: coinbase spot mid at window_start_ts (the up/down strike).
LEFT JOIN LATERAL (
    SELECT mid AS start_mid FROM orderbook_snapshots
    WHERE venue='coinbase' AND product='BTC-USD' AND ts <= s.window_start_ts
    ORDER BY ts DESC LIMIT 1
) ss ON TRUE

LEFT JOIN LATERAL (
    SELECT imbalance, spread, mid, best_bid, best_ask, bid_volume_topN, ask_volume_topN
    FROM orderbook_snapshots
    WHERE venue='bybit' AND product='BTCUSDT' AND ts <= s.decision_ts
    ORDER BY ts DESC LIMIT 1
) ob_b ON TRUE
LEFT JOIN LATERAL (
    SELECT imbalance, spread, mid, best_bid, best_ask, bid_volume_topN, ask_volume_topN
    FROM orderbook_snapshots
    WHERE venue='coinbase' AND product='BTC-USD' AND ts <= s.decision_ts
    ORDER BY ts DESC LIMIT 1
) ob_c ON TRUE
LEFT JOIN LATERAL (
    SELECT basis FROM mark_index_prices
    WHERE venue='bybit' AND product='BTCUSDT' AND ts <= s.decision_ts
    ORDER BY ts DESC LIMIT 1
) mi ON TRUE
LEFT JOIN LATERAL (
    SELECT ts FROM funding_history
    WHERE venue='bybit' AND product='BTCUSDT' AND ts <= s.decision_ts
    ORDER BY ts DESC LIMIT 1
) fh ON TRUE

-- Aggressor flow, bybit, four horizons.
LEFT JOIN LATERAL (
    SELECT CASE WHEN COALESCE(SUM(size),0)>0
                THEN COALESCE(SUM(CASE WHEN side IN ('buy','Buy') THEN size ELSE -size END),0)/SUM(size)
                ELSE NULL END AS flow_ratio
    FROM trade_prints WHERE venue='bybit' AND product='BTCUSDT'
      AND ts BETWEEN s.decision_ts - INTERVAL '10 seconds' AND s.decision_ts
) tf_b_10s ON TRUE
LEFT JOIN LATERAL (
    SELECT CASE WHEN COALESCE(SUM(size),0)>0
                THEN COALESCE(SUM(CASE WHEN side IN ('buy','Buy') THEN size ELSE -size END),0)/SUM(size)
                ELSE NULL END AS flow_ratio
    FROM trade_prints WHERE venue='bybit' AND product='BTCUSDT'
      AND ts BETWEEN s.decision_ts - INTERVAL '60 seconds' AND s.decision_ts
) tf_b_60s ON TRUE
LEFT JOIN LATERAL (
    SELECT CASE WHEN COALESCE(SUM(size),0)>0
                THEN COALESCE(SUM(CASE WHEN side IN ('buy','Buy') THEN size ELSE -size END),0)/SUM(size)
                ELSE NULL END AS flow_ratio
    FROM trade_prints WHERE venue='bybit' AND product='BTCUSDT'
      AND ts BETWEEN s.decision_ts - INTERVAL '300 seconds' AND s.decision_ts
) tf_b_300s ON TRUE
LEFT JOIN LATERAL (
    SELECT CASE WHEN COALESCE(SUM(size),0)>0
                THEN COALESCE(SUM(CASE WHEN side IN ('buy','Buy') THEN size ELSE -size END),0)/SUM(size)
                ELSE NULL END AS flow_ratio
    FROM trade_prints WHERE venue='bybit' AND product='BTCUSDT'
      AND ts BETWEEN s.decision_ts - INTERVAL '900 seconds' AND s.decision_ts
) tf_b_900s ON TRUE

-- Aggressor flow, coinbase, four horizons.
LEFT JOIN LATERAL (
    SELECT CASE WHEN COALESCE(SUM(size),0)>0
                THEN COALESCE(SUM(CASE WHEN side IN ('buy','Buy') THEN size ELSE -size END),0)/SUM(size)
                ELSE NULL END AS flow_ratio
    FROM trade_prints WHERE venue='coinbase' AND product='BTC-USD'
      AND ts BETWEEN s.decision_ts - INTERVAL '10 seconds' AND s.decision_ts
) tf_c_10s ON TRUE
LEFT JOIN LATERAL (
    SELECT CASE WHEN COALESCE(SUM(size),0)>0
                THEN COALESCE(SUM(CASE WHEN side IN ('buy','Buy') THEN size ELSE -size END),0)/SUM(size)
                ELSE NULL END AS flow_ratio
    FROM trade_prints WHERE venue='coinbase' AND product='BTC-USD'
      AND ts BETWEEN s.decision_ts - INTERVAL '60 seconds' AND s.decision_ts
) tf_c_60s ON TRUE
LEFT JOIN LATERAL (
    SELECT CASE WHEN COALESCE(SUM(size),0)>0
                THEN COALESCE(SUM(CASE WHEN side IN ('buy','Buy') THEN size ELSE -size END),0)/SUM(size)
                ELSE NULL END AS flow_ratio
    FROM trade_prints WHERE venue='coinbase' AND product='BTC-USD'
      AND ts BETWEEN s.decision_ts - INTERVAL '300 seconds' AND s.decision_ts
) tf_c_300s ON TRUE
LEFT JOIN LATERAL (
    SELECT CASE WHEN COALESCE(SUM(size),0)>0
                THEN COALESCE(SUM(CASE WHEN side IN ('buy','Buy') THEN size ELSE -size END),0)/SUM(size)
                ELSE NULL END AS flow_ratio
    FROM trade_prints WHERE venue='coinbase' AND product='BTC-USD'
      AND ts BETWEEN s.decision_ts - INTERVAL '900 seconds' AND s.decision_ts
) tf_c_900s ON TRUE

-- OFI 60s (net top-of-book size change) per venue.
LEFT JOIN LATERAL (
    SELECT
      (SELECT bid_volume_topN FROM orderbook_snapshots
         WHERE venue='bybit' AND product='BTCUSDT'
           AND ts BETWEEN s.decision_ts - INTERVAL '60 seconds' AND s.decision_ts
         ORDER BY ts DESC LIMIT 1) -
      (SELECT bid_volume_topN FROM orderbook_snapshots
         WHERE venue='bybit' AND product='BTCUSDT'
           AND ts BETWEEN s.decision_ts - INTERVAL '60 seconds' AND s.decision_ts
         ORDER BY ts ASC LIMIT 1) -
      ((SELECT ask_volume_topN FROM orderbook_snapshots
         WHERE venue='bybit' AND product='BTCUSDT'
           AND ts BETWEEN s.decision_ts - INTERVAL '60 seconds' AND s.decision_ts
         ORDER BY ts DESC LIMIT 1) -
       (SELECT ask_volume_topN FROM orderbook_snapshots
         WHERE venue='bybit' AND product='BTCUSDT'
           AND ts BETWEEN s.decision_ts - INTERVAL '60 seconds' AND s.decision_ts
         ORDER BY ts ASC LIMIT 1)) AS ofi_60s
) ofi_b ON TRUE
LEFT JOIN LATERAL (
    SELECT
      (SELECT bid_volume_topN FROM orderbook_snapshots
         WHERE venue='coinbase' AND product='BTC-USD'
           AND ts BETWEEN s.decision_ts - INTERVAL '60 seconds' AND s.decision_ts
         ORDER BY ts DESC LIMIT 1) -
      (SELECT bid_volume_topN FROM orderbook_snapshots
         WHERE venue='coinbase' AND product='BTC-USD'
           AND ts BETWEEN s.decision_ts - INTERVAL '60 seconds' AND s.decision_ts
         ORDER BY ts ASC LIMIT 1) -
      ((SELECT ask_volume_topN FROM orderbook_snapshots
         WHERE venue='coinbase' AND product='BTC-USD'
           AND ts BETWEEN s.decision_ts - INTERVAL '60 seconds' AND s.decision_ts
         ORDER BY ts DESC LIMIT 1) -
       (SELECT ask_volume_topN FROM orderbook_snapshots
         WHERE venue='coinbase' AND product='BTC-USD'
           AND ts BETWEEN s.decision_ts - INTERVAL '60 seconds' AND s.decision_ts
         ORDER BY ts ASC LIMIT 1)) AS ofi_60s
) ofi_c ON TRUE

-- Realized vol from coinbase mid, three horizons.
LEFT JOIN LATERAL (
    SELECT STDDEV(LN(mid / NULLIF(prev_mid,0))) AS rv FROM (
      SELECT mid, LAG(mid) OVER (ORDER BY ts) AS prev_mid
      FROM orderbook_snapshots WHERE venue='coinbase' AND product='BTC-USD'
        AND ts BETWEEN s.decision_ts - INTERVAL '60 seconds' AND s.decision_ts AND mid > 0
    ) x WHERE prev_mid IS NOT NULL
) rv_c_60 ON TRUE
LEFT JOIN LATERAL (
    SELECT STDDEV(LN(mid / NULLIF(prev_mid,0))) AS rv FROM (
      SELECT mid, LAG(mid) OVER (ORDER BY ts) AS prev_mid
      FROM orderbook_snapshots WHERE venue='coinbase' AND product='BTC-USD'
        AND ts BETWEEN s.decision_ts - INTERVAL '300 seconds' AND s.decision_ts AND mid > 0
    ) x WHERE prev_mid IS NOT NULL
) rv_c_300 ON TRUE
LEFT JOIN LATERAL (
    SELECT STDDEV(LN(mid / NULLIF(prev_mid,0))) AS rv FROM (
      SELECT mid, LAG(mid) OVER (ORDER BY ts) AS prev_mid
      FROM orderbook_snapshots WHERE venue='coinbase' AND product='BTC-USD'
        AND ts BETWEEN s.decision_ts - INTERVAL '900 seconds' AND s.decision_ts AND mid > 0
    ) x WHERE prev_mid IS NOT NULL
) rv_c_900 ON TRUE

-- Jump indicator (BNS): RV and BV over last 5m of coinbase mid returns.
LEFT JOIN LATERAL (
    SELECT
      SUM(r*r) AS rv,
      (PI()/2) * SUM(ABS(r) * ABS(prev_r)) AS bv
    FROM (
      SELECT r, LAG(r) OVER (ORDER BY ts) AS prev_r FROM (
        SELECT ts, LN(mid / NULLIF(LAG(mid) OVER (ORDER BY ts),0)) AS r
        FROM orderbook_snapshots WHERE venue='coinbase' AND product='BTC-USD'
          AND ts BETWEEN s.decision_ts - INTERVAL '300 seconds' AND s.decision_ts AND mid > 0
      ) a WHERE r IS NOT NULL
    ) b WHERE prev_r IS NOT NULL
) jump ON TRUE

-- VPIN (50 equal-volume buckets) over last 5m of bybit trade prints.
LEFT JOIN LATERAL (
    SELECT AVG(abs_signed / NULLIF(vol,0)) AS vpin_50 FROM (
      SELECT bucket, ABS(SUM(signed)) AS abs_signed, SUM(sz) AS vol FROM (
        SELECT
          LEAST(FLOOR(SUM(size) OVER (ORDER BY ts) / NULLIF(tot.total/50.0,0)), 49) AS bucket,
          size AS sz,
          CASE WHEN side IN ('buy','Buy') THEN size ELSE -size END AS signed
        FROM trade_prints tp
        CROSS JOIN (
          SELECT SUM(size) AS total FROM trade_prints
          WHERE venue='bybit' AND product='BTCUSDT'
            AND ts BETWEEN s.decision_ts - INTERVAL '300 seconds' AND s.decision_ts
        ) tot
        WHERE tp.venue='bybit' AND tp.product='BTCUSDT'
          AND tp.ts BETWEEN s.decision_ts - INTERVAL '300 seconds' AND s.decision_ts
          AND tot.total > 0
      ) bucketed GROUP BY bucket
    ) per_bucket
) vpin ON TRUE

-- Multi-horizon mids for returns (coinbase).
LEFT JOIN LATERAL (
    SELECT mid AS mid_then FROM orderbook_snapshots
    WHERE venue='coinbase' AND product='BTC-USD' AND ts <= s.decision_ts - INTERVAL '10 seconds'
    ORDER BY ts DESC LIMIT 1
) ret_c_10 ON TRUE
LEFT JOIN LATERAL (
    SELECT mid AS mid_then FROM orderbook_snapshots
    WHERE venue='coinbase' AND product='BTC-USD' AND ts <= s.decision_ts - INTERVAL '60 seconds'
    ORDER BY ts DESC LIMIT 1
) ret_c_60 ON TRUE
LEFT JOIN LATERAL (
    SELECT mid AS mid_then FROM orderbook_snapshots
    WHERE venue='coinbase' AND product='BTC-USD' AND ts <= s.decision_ts - INTERVAL '300 seconds'
    ORDER BY ts DESC LIMIT 1
) ret_c_300 ON TRUE

-- Spread-regime medians (1h).
LEFT JOIN LATERAL (
    SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY spread) AS median_spread
    FROM orderbook_snapshots WHERE venue='bybit' AND product='BTCUSDT'
      AND ts BETWEEN s.decision_ts - INTERVAL '1 hour' AND s.decision_ts
) sr_b ON TRUE
LEFT JOIN LATERAL (
    SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY spread) AS median_spread
    FROM orderbook_snapshots WHERE venue='coinbase' AND product='BTC-USD'
      AND ts BETWEEN s.decision_ts - INTERVAL '1 hour' AND s.decision_ts
) sr_c ON TRUE

WHERE s.outcome IS NOT NULL
ORDER BY s.decision_ts;

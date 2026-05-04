-- db/analytics/high_edge_clustering.sql
-- Diagnostic: where do |edge| >= 0.15 trades cluster, and is the toxicity
-- concentrated in a specific vol regime / window / asset / side / market?
--
-- Context: by-edge calibration showed 0.15-0.20 ROI = -14.7% and 0.20+ = -6.0%,
-- while 0.05-0.10 ROI = +9.5%. Goal: find the slice that's poisoning the
-- high-edge bucket so we can decide between a hot-reloadable max-edge ceiling,
-- a vol-regime gate, or a deeper estimator/orderbook-staleness fix.
--
-- Run on prod:    psql "$PROD_DATABASE_URL" -f db/analytics/high_edge_clustering.sql
-- Or paste blocks individually into psql.

\echo
\echo === 1. High-edge (|edge| >= 0.15) by vol regime ===
SELECT
    CASE
        WHEN vol_at_decision < 0.30 THEN '1. <0.30 calm'
        WHEN vol_at_decision < 0.60 THEN '2. 0.30-0.60 normal'
        WHEN vol_at_decision < 1.00 THEN '3. 0.60-1.00 active'
        ELSE                              '4. 1.00+ extreme'
    END                                                  AS vol_bucket,
    COUNT(*)                                             AS trades,
    COUNT(*) FILTER (WHERE won)                          AS wins,
    ROUND(100.0 * COUNT(*) FILTER (WHERE won)
          / NULLIF(COUNT(*), 0), 1)                      AS win_pct,
    ROUND(AVG(abs_edge)::numeric, 4)                     AS avg_edge,
    ROUND(SUM(size)::numeric, 2)                         AS staked,
    ROUND(SUM(pnl)::numeric, 2)                          AS total_pnl,
    ROUND((100.0 * SUM(pnl) / NULLIF(SUM(size), 0))::numeric, 1) AS roi_pct
FROM quant_short_v
WHERE pnl IS NOT NULL AND abs_edge >= 0.15
GROUP BY vol_bucket
ORDER BY vol_bucket;

\echo
\echo === 2. High-edge by window duration ===
SELECT
    window_minutes                                       AS window_min,
    COUNT(*)                                             AS trades,
    COUNT(*) FILTER (WHERE won)                          AS wins,
    ROUND(100.0 * COUNT(*) FILTER (WHERE won)
          / NULLIF(COUNT(*), 0), 1)                      AS win_pct,
    ROUND(AVG(abs_edge)::numeric, 4)                     AS avg_edge,
    ROUND(SUM(size)::numeric, 2)                         AS staked,
    ROUND(SUM(pnl)::numeric, 2)                          AS total_pnl,
    ROUND((100.0 * SUM(pnl) / NULLIF(SUM(size), 0))::numeric, 1) AS roi_pct
FROM quant_short_v
WHERE pnl IS NOT NULL AND abs_edge >= 0.15
GROUP BY window_minutes
ORDER BY window_minutes;

\echo
\echo === 3. High-edge by asset ===
SELECT
    asset_id,
    COUNT(*)                                             AS trades,
    COUNT(*) FILTER (WHERE won)                          AS wins,
    ROUND(100.0 * COUNT(*) FILTER (WHERE won)
          / NULLIF(COUNT(*), 0), 1)                      AS win_pct,
    ROUND(AVG(abs_edge)::numeric, 4)                     AS avg_edge,
    ROUND(SUM(size)::numeric, 2)                         AS staked,
    ROUND(SUM(pnl)::numeric, 2)                          AS total_pnl,
    ROUND((100.0 * SUM(pnl) / NULLIF(SUM(size), 0))::numeric, 1) AS roi_pct
FROM quant_short_v
WHERE pnl IS NOT NULL AND abs_edge >= 0.15
GROUP BY asset_id
ORDER BY asset_id;

\echo
\echo === 4. High-edge by side ===
SELECT
    side,
    COUNT(*)                                             AS trades,
    COUNT(*) FILTER (WHERE won)                          AS wins,
    ROUND(100.0 * COUNT(*) FILTER (WHERE won)
          / NULLIF(COUNT(*), 0), 1)                      AS win_pct,
    ROUND(AVG(abs_edge)::numeric, 4)                     AS avg_edge,
    ROUND(SUM(size)::numeric, 2)                         AS staked,
    ROUND(SUM(pnl)::numeric, 2)                          AS total_pnl,
    ROUND((100.0 * SUM(pnl) / NULLIF(SUM(size), 0))::numeric, 1) AS roi_pct
FROM quant_short_v
WHERE pnl IS NOT NULL AND abs_edge >= 0.15
GROUP BY side
ORDER BY side;

\echo
\echo === 5. High-edge by side x vol regime (catches NO-side leak in calm vol) ===
SELECT
    side,
    CASE
        WHEN vol_at_decision < 0.30 THEN '1. <0.30 calm'
        WHEN vol_at_decision < 0.60 THEN '2. 0.30-0.60 normal'
        WHEN vol_at_decision < 1.00 THEN '3. 0.60-1.00 active'
        ELSE                              '4. 1.00+ extreme'
    END                                                  AS vol_bucket,
    COUNT(*)                                             AS trades,
    ROUND(100.0 * COUNT(*) FILTER (WHERE won)
          / NULLIF(COUNT(*), 0), 1)                      AS win_pct,
    ROUND(SUM(pnl)::numeric, 2)                          AS total_pnl,
    ROUND((100.0 * SUM(pnl) / NULLIF(SUM(size), 0))::numeric, 1) AS roi_pct
FROM quant_short_v
WHERE pnl IS NOT NULL AND abs_edge >= 0.15
GROUP BY side, vol_bucket
ORDER BY side, vol_bucket;

\echo
\echo === 6. High-edge by moneyness at decision ===
\echo --   moneyness = log(start_spot / spot_at_decision); near 0 means decision
\echo --   was made when spot was already at the strike (window probably stale).
\echo --   Wide |moneyness| means decision happened far from the strike line.
SELECT
    CASE
        WHEN ABS(LN(spot_at_decision / NULLIF(start_spot, 0))) < 0.0010 THEN '1. <0.10% (at strike)'
        WHEN ABS(LN(spot_at_decision / NULLIF(start_spot, 0))) < 0.0030 THEN '2. 0.10-0.30%'
        WHEN ABS(LN(spot_at_decision / NULLIF(start_spot, 0))) < 0.0080 THEN '3. 0.30-0.80%'
        ELSE                                                                 '4. 0.80%+'
    END                                                  AS moneyness_bucket,
    COUNT(*)                                             AS trades,
    ROUND(100.0 * COUNT(*) FILTER (WHERE won)
          / NULLIF(COUNT(*), 0), 1)                      AS win_pct,
    ROUND(AVG(abs_edge)::numeric, 4)                     AS avg_edge,
    ROUND(SUM(pnl)::numeric, 2)                          AS total_pnl,
    ROUND((100.0 * SUM(pnl) / NULLIF(SUM(size), 0))::numeric, 1) AS roi_pct
FROM quant_short_v
WHERE pnl IS NOT NULL AND abs_edge >= 0.15
  AND start_spot IS NOT NULL AND spot_at_decision IS NOT NULL
GROUP BY moneyness_bucket
ORDER BY moneyness_bucket;

\echo
\echo === 7. High-edge by time-to-resolution at decision ===
\echo --   short TTR + high edge = either a genuine info edge or a stale-quote
\echo --   artifact. Long TTR + high edge = estimator overconfidence.
SELECT
    CASE
        WHEN EXTRACT(EPOCH FROM (window_end_ts - decision_ts)) < 60   THEN '1. <1m'
        WHEN EXTRACT(EPOCH FROM (window_end_ts - decision_ts)) < 180  THEN '2. 1-3m'
        WHEN EXTRACT(EPOCH FROM (window_end_ts - decision_ts)) < 600  THEN '3. 3-10m'
        ELSE                                                               '4. 10m+'
    END                                                  AS ttr_bucket,
    COUNT(*)                                             AS trades,
    ROUND(100.0 * COUNT(*) FILTER (WHERE won)
          / NULLIF(COUNT(*), 0), 1)                      AS win_pct,
    ROUND(AVG(abs_edge)::numeric, 4)                     AS avg_edge,
    ROUND(SUM(pnl)::numeric, 2)                          AS total_pnl,
    ROUND((100.0 * SUM(pnl) / NULLIF(SUM(size), 0))::numeric, 1) AS roi_pct
FROM quant_short_v
WHERE pnl IS NOT NULL AND abs_edge >= 0.15
GROUP BY ttr_bucket
ORDER BY ttr_bucket;

\echo
\echo === 8. Side x window x asset (catches a single bad cell) ===
SELECT
    asset_id,
    window_minutes                                       AS window_min,
    side,
    COUNT(*)                                             AS trades,
    ROUND(100.0 * COUNT(*) FILTER (WHERE won)
          / NULLIF(COUNT(*), 0), 1)                      AS win_pct,
    ROUND(SUM(pnl)::numeric, 2)                          AS total_pnl,
    ROUND((100.0 * SUM(pnl) / NULLIF(SUM(size), 0))::numeric, 1) AS roi_pct
FROM quant_short_v
WHERE pnl IS NOT NULL AND abs_edge >= 0.15
GROUP BY asset_id, window_minutes, side
HAVING COUNT(*) >= 5
ORDER BY roi_pct ASC NULLS LAST;

\echo
\echo === 9. Worst 20 high-edge trades (by P&L) ===
SELECT
    decision_ts AT TIME ZONE 'UTC'                       AS decision_utc,
    asset_id,
    window_minutes                                       AS w_min,
    side,
    ROUND(estimator_p_up::numeric, 3)                    AS p_up,
    ROUND(fill_price_assumed::numeric, 3)                AS fill_px,
    ROUND(edge_at_decision::numeric, 3)                  AS edge,
    ROUND(vol_at_decision::numeric, 3)                   AS vol,
    ROUND(size::numeric, 2)                              AS size,
    ROUND(pnl::numeric, 2)                               AS pnl,
    won,
    slug
FROM quant_short_v
WHERE pnl IS NOT NULL AND abs_edge >= 0.15
ORDER BY pnl ASC
LIMIT 20;

\echo
\echo === 10. Recency check: high-edge ROI by week (did SELL->BUY-NO fix help?) ===
SELECT
    DATE_TRUNC('week', decision_ts)::date                AS week,
    COUNT(*)                                             AS trades,
    ROUND(100.0 * COUNT(*) FILTER (WHERE won)
          / NULLIF(COUNT(*), 0), 1)                      AS win_pct,
    ROUND(SUM(pnl)::numeric, 2)                          AS total_pnl,
    ROUND((100.0 * SUM(pnl) / NULLIF(SUM(size), 0))::numeric, 1) AS roi_pct
FROM quant_short_v
WHERE pnl IS NOT NULL AND abs_edge >= 0.15
GROUP BY week
ORDER BY week;

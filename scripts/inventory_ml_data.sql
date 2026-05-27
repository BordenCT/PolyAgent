-- Inventory of usable ML labels for the microstructure-estimator.
-- Answers: do we have enough (features at decision_ts -> outcome) rows?
--
-- Three populations:
--   1. TRADED:  quant_short_trades that resolved (current pre-reg gate).
--   2. SHADOW:  quant_decider_rejections where p_up was computed (reached
--               the estimator) AND the market later resolved. These are
--               decision points the bankroll freeze never throttled.
--   3. OB-COVERED subset of each: decision_ts has a bybit OB snapshot
--               within 60s (the join requirement for microstructure feats).

\echo === 1. TRADED population ===
SELECT
    COUNT(*)                                              AS resolved_trades,
    COUNT(*) FILTER (WHERE EXISTS (
        SELECT 1 FROM orderbook_snapshots ob
        WHERE ob.venue='bybit' AND ob.product='BTCUSDT'
          AND ob.ts <= t.decision_ts
          AND ob.ts >= t.decision_ts - INTERVAL '60 seconds'
    ))                                                    AS ob_covered,
    MIN(t.decision_ts)                                    AS first_ts,
    MAX(t.decision_ts)                                    AS last_ts
FROM quant_short_v t
WHERE t.pnl IS NOT NULL;

\echo === 2. SHADOW population (rejections that reached the estimator) ===
-- p_up IS NOT NULL means the rejection fired at or after the edge gate,
-- so estimator_p_up exists. Join to markets by polymarket_id to get the
-- realized outcome. Only count rows whose market resolved (outcome set).
SELECT
    COUNT(*)                                              AS rejections_with_p_up,
    COUNT(*) FILTER (WHERE m.outcome IS NOT NULL)         AS resolved,
    COUNT(*) FILTER (WHERE m.outcome IS NOT NULL AND EXISTS (
        SELECT 1 FROM orderbook_snapshots ob
        WHERE ob.venue='bybit' AND ob.product='BTCUSDT'
          AND ob.ts <= r.decision_ts
          AND ob.ts >= r.decision_ts - INTERVAL '60 seconds'
    ))                                                    AS resolved_ob_covered,
    MIN(r.decision_ts)                                    AS first_ts,
    MAX(r.decision_ts)                                    AS last_ts
FROM quant_decider_rejections r
LEFT JOIN quant_short_markets m ON m.polymarket_id = r.polymarket_id
WHERE r.p_up IS NOT NULL;

\echo === 3. Rejections breakdown by reason (which have p_up) ===
SELECT
    reason,
    COUNT(*)                                  AS n,
    COUNT(*) FILTER (WHERE p_up IS NOT NULL)  AS with_p_up
FROM quant_decider_rejections
GROUP BY reason
ORDER BY n DESC;

\echo === 4. COMBINED unique decision points with OB coverage + outcome ===
-- Union of traded + shadow, deduped by (polymarket_id, decision_ts bucket).
-- This is the candidate ML row count if we adopt the evaluated-markets
-- population (requires pre-reg amendment).
WITH labeled AS (
    SELECT t.polymarket_id, t.decision_ts, t.outcome
    FROM quant_short_v t
    WHERE t.pnl IS NOT NULL
    UNION ALL
    SELECT r.polymarket_id, r.decision_ts, m.outcome
    FROM quant_decider_rejections r
    JOIN quant_short_markets m ON m.polymarket_id = r.polymarket_id
    WHERE r.p_up IS NOT NULL AND m.outcome IS NOT NULL
)
SELECT
    COUNT(*)                                              AS total_labeled,
    COUNT(*) FILTER (WHERE EXISTS (
        SELECT 1 FROM orderbook_snapshots ob
        WHERE ob.venue='bybit' AND ob.product='BTCUSDT'
          AND ob.ts <= labeled.decision_ts
          AND ob.ts >= labeled.decision_ts - INTERVAL '60 seconds'
    ))                                                    AS ob_covered,
    COUNT(DISTINCT polymarket_id)                         AS distinct_markets,
    SUM(CASE WHEN outcome='YES' THEN 1 ELSE 0 END)        AS yes_outcomes,
    SUM(CASE WHEN outcome='NO'  THEN 1 ELSE 0 END)        AS no_outcomes
FROM labeled;

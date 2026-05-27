-- Determine whether shadow decision points (rejections with p_up) can be
-- unlocked by re-resolving persisted markets, or need full reconstruction
-- from the slug + Polymarket outcome.

\echo === Are the rejected markets persisted in quant_short_markets? ===
SELECT
    COUNT(DISTINCT r.polymarket_id)                                   AS distinct_rejection_pmids,
    COUNT(DISTINCT m.polymarket_id)                                   AS matched_in_markets_tbl,
    COUNT(DISTINCT m.polymarket_id) FILTER (WHERE m.outcome IS NOT NULL)
                                                                      AS matched_and_resolved,
    COUNT(DISTINCT m.id) FILTER (
        WHERE m.outcome IS NULL AND m.window_end_ts < NOW()
    )                                                                 AS persisted_unresolved_past_end
FROM quant_decider_rejections r
LEFT JOIN quant_short_markets m ON m.polymarket_id = r.polymarket_id
WHERE r.p_up IS NOT NULL;

\echo === Do the rejection rows carry a parseable slug? ===
SELECT
    COUNT(*)                                          AS rows_with_p_up,
    COUNT(*) FILTER (WHERE slug ~ '-updown-')         AS slug_looks_parseable,
    COUNT(*) FILTER (WHERE polymarket_id IS NOT NULL) AS has_condition_id
FROM quant_decider_rejections
WHERE p_up IS NOT NULL;

\echo === OB coverage of shadow decision timestamps ===
SELECT
    COUNT(*)                                          AS rows_with_p_up,
    COUNT(*) FILTER (WHERE EXISTS (
        SELECT 1 FROM orderbook_snapshots ob
        WHERE ob.venue='bybit' AND ob.product='BTCUSDT'
          AND ob.ts <= r.decision_ts
          AND ob.ts >= r.decision_ts - INTERVAL '60 seconds'
    ))                                                AS ob_covered
FROM quant_decider_rejections r
WHERE p_up IS NOT NULL;

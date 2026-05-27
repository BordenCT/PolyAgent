WITH shadow AS (
    SELECT DISTINCT ON (r.polymarket_id)
        r.polymarket_id,
        r.slug,
        r.decision_ts
    FROM quant_decider_rejections r
    WHERE r.p_up IS NOT NULL
      AND r.polymarket_id IS NOT NULL
    ORDER BY r.polymarket_id, r.decision_ts DESC
)
SELECT
    COUNT(*) AS distinct_markets,
    COUNT(*) FILTER (WHERE slug ~ '-updown-[0-9]+[mhd]-[0-9]+$') AS parseable_slug,
    COUNT(*) FILTER (WHERE EXISTS (
        SELECT 1 FROM orderbook_snapshots ob
        WHERE ob.venue = 'bybit' AND ob.product = 'BTCUSDT'
          AND ob.ts <= shadow.decision_ts
          AND ob.ts >= shadow.decision_ts - INTERVAL '60 seconds'
    )) AS ob_covered,
    COUNT(*) FILTER (
        WHERE slug ~ '-updown-[0-9]+[mhd]-[0-9]+$'
          AND EXISTS (
            SELECT 1 FROM orderbook_snapshots ob
            WHERE ob.venue = 'bybit' AND ob.product = 'BTCUSDT'
              AND ob.ts <= shadow.decision_ts
              AND ob.ts >= shadow.decision_ts - INTERVAL '60 seconds'
          )
    ) AS usable_candidates,
    MIN(decision_ts) AS first_ts,
    MAX(decision_ts) AS last_ts
FROM shadow;

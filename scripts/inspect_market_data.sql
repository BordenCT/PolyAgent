-- Inventory the market_data tables so we know what's joinable to the
-- quant_short trades window (2026-05-05 .. 2026-05-13).

\echo === orderbook_snapshots ===
SELECT venue, product, COUNT(*) AS rows,
       MIN(ts) AS first_ts, MAX(ts) AS last_ts,
       ROUND(AVG(spread)::numeric, 4) AS avg_spread,
       ROUND(AVG(imbalance)::numeric, 4) AS avg_imbalance
FROM orderbook_snapshots
GROUP BY venue, product
ORDER BY venue, product;

\echo === trade_prints ===
SELECT venue, product, COUNT(*) AS rows,
       MIN(ts) AS first_ts, MAX(ts) AS last_ts,
       COUNT(*) FILTER (WHERE side = 'buy')  AS buys,
       COUNT(*) FILTER (WHERE side = 'sell') AS sells
FROM trade_prints
GROUP BY venue, product
ORDER BY venue, product;

\echo === funding_history ===
SELECT venue, product, COUNT(*) AS rows,
       MIN(ts) AS first_ts, MAX(ts) AS last_ts,
       ROUND(AVG(funding_rate)::numeric, 6) AS avg_rate,
       ROUND(MIN(funding_rate)::numeric, 6) AS min_rate,
       ROUND(MAX(funding_rate)::numeric, 6) AS max_rate
FROM funding_history
GROUP BY venue, product
ORDER BY venue, product;

\echo === mark_index_prices ===
SELECT venue, product, COUNT(*) AS rows,
       MIN(ts) AS first_ts, MAX(ts) AS last_ts,
       ROUND(AVG(basis)::numeric, 4) AS avg_basis
FROM mark_index_prices
GROUP BY venue, product
ORDER BY venue, product;

\echo === coverage near decision_ts window (2026-05-05 .. 2026-05-13) ===
SELECT 'orderbook_snapshots' AS tbl, COUNT(*) AS rows_in_window
FROM orderbook_snapshots
WHERE ts BETWEEN '2026-05-05' AND '2026-05-14'
UNION ALL
SELECT 'trade_prints', COUNT(*)
FROM trade_prints
WHERE ts BETWEEN '2026-05-05' AND '2026-05-14'
UNION ALL
SELECT 'funding_history', COUNT(*)
FROM funding_history
WHERE ts BETWEEN '2026-05-05' AND '2026-05-14'
UNION ALL
SELECT 'mark_index_prices', COUNT(*)
FROM mark_index_prices
WHERE ts BETWEEN '2026-05-05' AND '2026-05-14';

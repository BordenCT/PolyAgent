-- db/migrations/008_polyagent_trades_view.sql
-- Phase 2 (view-based): unified read view across the main positions ledger
-- and the short-horizon paper ledger.
--
-- The original Phase 2 plan called for a physical merge of quant_short_*
-- into the positions table. This view-only approach delivers the same
-- single-number analytics (combined trade count, P&L, win rate) without
-- backfilling 100s of rows, dual-writing, or touching the gate-eval logic
-- that still reads `positions` directly. Reversible.
--
-- ledger discriminator:
--   'main'           -> positions table (Claude-brain strike service)
--   'short_horizon'  -> quant_short_trades via quant_short_v (paper)

CREATE OR REPLACE VIEW polyagent_trades_v AS
SELECT
    'main'::text             AS ledger,
    p.id                     AS trade_id,
    p.market_id,
    p.side,
    p.position_size          AS size,
    p.pnl,
    (p.pnl > 0)              AS won,
    p.paper_trade,
    p.opened_at              AS started_at,
    p.closed_at              AS resolved_at
FROM positions p
WHERE p.status = 'closed'

UNION ALL

SELECT
    'short_horizon'::text    AS ledger,
    qsv.trade_id,
    qsv.market_id,
    qsv.side,
    qsv.size,
    qsv.pnl,
    qsv.won,
    TRUE                     AS paper_trade,
    qsv.decision_ts          AS started_at,
    qsv.trade_resolved_at    AS resolved_at
FROM quant_short_v qsv
WHERE qsv.pnl IS NOT NULL;

-- Wipes paper trading state while preserving learned priors and seed data.
-- Run while the bot is stopped.
--
-- Preserves: historical_outcomes, target_wallets, backtest_runs, backtest_positions
-- Wipes: markets, thesis, positions, trade_log

BEGIN;

TRUNCATE TABLE trade_log RESTART IDENTITY CASCADE;
TRUNCATE TABLE positions RESTART IDENTITY CASCADE;
TRUNCATE TABLE thesis    RESTART IDENTITY CASCADE;
TRUNCATE TABLE markets   RESTART IDENTITY CASCADE;

COMMIT;

\echo 'Paper state reset. markets/thesis/positions/trade_log are empty; historical_outcomes preserved.'

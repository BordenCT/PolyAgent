-- db/migrations/003_trade_log_and_live.sql
-- Add volume_at_entry for exit-monitor volume trigger + error column for trade_log

ALTER TABLE positions
    ADD COLUMN IF NOT EXISTS volume_at_entry DECIMAL NOT NULL DEFAULT 0;

ALTER TABLE trade_log
    ADD COLUMN IF NOT EXISTS error TEXT;

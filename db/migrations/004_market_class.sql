-- db/migrations/004_market_class.sql
-- Add market_class for per-class analytics and future per-class policies.

DO $$ BEGIN
    CREATE TYPE market_class AS ENUM ('sports', 'crypto', 'politics', 'macro', 'other');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE markets
    ADD COLUMN IF NOT EXISTS market_class market_class NOT NULL DEFAULT 'other';

CREATE INDEX IF NOT EXISTS idx_markets_class ON markets(market_class);

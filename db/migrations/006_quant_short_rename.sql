-- db/migrations/006_quant_short_rename.sql
-- Rename btc5m_* tables to quant_short_*; add asset_id and price_source_id.

ALTER TABLE btc5m_markets RENAME TO quant_short_markets;
ALTER TABLE btc5m_trades  RENAME TO quant_short_trades;

ALTER INDEX idx_btc5m_markets_window_end RENAME TO idx_quant_short_markets_window_end;
ALTER INDEX idx_btc5m_markets_outcome    RENAME TO idx_quant_short_markets_outcome;
ALTER INDEX idx_btc5m_markets_duration   RENAME TO idx_quant_short_markets_duration;
ALTER INDEX idx_btc5m_trades_market      RENAME TO idx_quant_short_trades_market;
ALTER INDEX idx_btc5m_trades_decision    RENAME TO idx_quant_short_trades_decision;

ALTER TABLE quant_short_markets ADD COLUMN asset_id TEXT NOT NULL DEFAULT 'BTC';
ALTER TABLE quant_short_markets ALTER COLUMN asset_id DROP DEFAULT;
ALTER TABLE quant_short_markets ADD COLUMN price_source_id TEXT;

CREATE INDEX idx_quant_short_markets_asset ON quant_short_markets(asset_id);

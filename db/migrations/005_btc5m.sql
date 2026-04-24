-- db/migrations/005_btc5m.sql
-- BTC short-horizon up/down paper-trading subsystem: markets and trades.

CREATE TABLE IF NOT EXISTS btc5m_markets (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    polymarket_id     TEXT UNIQUE NOT NULL,
    slug              TEXT UNIQUE NOT NULL,
    token_id_yes      TEXT NOT NULL,
    token_id_no       TEXT NOT NULL,
    window_duration_s INTEGER NOT NULL,
    window_start_ts   TIMESTAMPTZ NOT NULL,
    window_end_ts     TIMESTAMPTZ NOT NULL,
    start_spot        DECIMAL,
    end_spot          DECIMAL,
    outcome           TEXT,
    discovered_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_btc5m_markets_window_end ON btc5m_markets(window_end_ts);
CREATE INDEX IF NOT EXISTS idx_btc5m_markets_outcome    ON btc5m_markets(outcome);
CREATE INDEX IF NOT EXISTS idx_btc5m_markets_duration   ON btc5m_markets(window_duration_s);

CREATE TABLE IF NOT EXISTS btc5m_trades (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    market_id           UUID NOT NULL REFERENCES btc5m_markets(id),
    decision_ts         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    side                TEXT NOT NULL,
    fill_price_assumed  DECIMAL NOT NULL,
    size                DECIMAL NOT NULL,
    estimator_p_up      DECIMAL NOT NULL,
    spot_at_decision    DECIMAL NOT NULL,
    vol_at_decision     DECIMAL NOT NULL,
    edge_at_decision    DECIMAL NOT NULL,
    pnl                 DECIMAL,
    resolved_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_btc5m_trades_market   ON btc5m_trades(market_id);
CREATE INDEX IF NOT EXISTS idx_btc5m_trades_decision ON btc5m_trades(decision_ts DESC);

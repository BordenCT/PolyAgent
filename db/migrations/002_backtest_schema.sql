-- db/migrations/002_backtest_schema.sql
-- Backtest run tracking

CREATE TABLE IF NOT EXISTS backtest_runs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    date_start      DATE NOT NULL,
    date_end        DATE NOT NULL,
    estimator       TEXT NOT NULL,
    parameters      JSONB NOT NULL DEFAULT '{}',
    results         JSONB,
    total_trades    INTEGER DEFAULT 0,
    win_rate        DECIMAL DEFAULT 0,
    total_pnl       DECIMAL DEFAULT 0,
    sharpe          DECIMAL DEFAULT 0,
    max_drawdown    DECIMAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS backtest_positions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id          UUID NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
    polymarket_id   TEXT NOT NULL,
    question        TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'unknown',
    side            TEXT NOT NULL,
    entry_price     DECIMAL NOT NULL,
    exit_price      DECIMAL,
    target_price    DECIMAL NOT NULL,
    kelly_fraction  DECIMAL NOT NULL,
    position_size   DECIMAL NOT NULL,
    pnl             DECIMAL DEFAULT 0,
    exit_reason     TEXT,
    entry_date      TIMESTAMPTZ NOT NULL,
    exit_date       TIMESTAMPTZ,
    estimator_prob  DECIMAL NOT NULL,
    market_price    DECIMAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_backtest_positions_run_id ON backtest_positions(run_id);

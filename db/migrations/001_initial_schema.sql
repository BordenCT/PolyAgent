CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";

CREATE TABLE markets (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    polymarket_id   TEXT UNIQUE NOT NULL,
    question        TEXT NOT NULL,
    question_embedding vector(1024),
    category        TEXT NOT NULL DEFAULT 'unknown',
    token_id        TEXT NOT NULL,
    midpoint_price  DECIMAL NOT NULL,
    bids_depth      DECIMAL NOT NULL,
    asks_depth      DECIMAL NOT NULL,
    hours_to_resolution DECIMAL NOT NULL,
    volume_24h      DECIMAL NOT NULL DEFAULT 0,
    scanned_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    score           JSONB,
    status          TEXT NOT NULL DEFAULT 'queued'
);
CREATE INDEX idx_markets_status ON markets(status);
CREATE INDEX idx_markets_scanned_at ON markets(scanned_at DESC);

CREATE TABLE target_wallets (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    address         TEXT UNIQUE NOT NULL,
    total_trades    INTEGER NOT NULL DEFAULT 0,
    win_rate        DECIMAL NOT NULL DEFAULT 0,
    total_pnl       DECIMAL NOT NULL DEFAULT 0,
    wallet_embedding vector(1024),
    discovered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE thesis (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    market_id       UUID NOT NULL REFERENCES markets(id),
    thesis_embedding vector(1024),
    claude_estimate DECIMAL NOT NULL,
    confidence      DECIMAL NOT NULL,
    checks          JSONB NOT NULL,
    checks_passed   INTEGER NOT NULL,
    thesis_text     TEXT NOT NULL,
    strategy_votes  JSONB NOT NULL DEFAULT '{}',
    consensus       TEXT NOT NULL DEFAULT 'none',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_thesis_market_id ON thesis(market_id);
CREATE INDEX idx_thesis_created_at ON thesis(created_at DESC);

CREATE TABLE positions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    thesis_id       UUID NOT NULL REFERENCES thesis(id),
    market_id       UUID NOT NULL REFERENCES markets(id),
    side            TEXT NOT NULL,
    entry_price     DECIMAL NOT NULL,
    target_price    DECIMAL NOT NULL,
    kelly_fraction  DECIMAL NOT NULL,
    position_size   DECIMAL NOT NULL,
    current_price   DECIMAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    exit_reason     TEXT,
    pnl             DECIMAL NOT NULL DEFAULT 0,
    paper_trade     BOOLEAN NOT NULL DEFAULT TRUE,
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at       TIMESTAMPTZ
);
CREATE INDEX idx_positions_status ON positions(status);
CREATE INDEX idx_positions_market_id ON positions(market_id);

CREATE TABLE trade_log (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    position_id     UUID NOT NULL REFERENCES positions(id),
    action          TEXT NOT NULL,
    reason          TEXT,
    raw_request     JSONB,
    raw_response    JSONB,
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_trade_log_position_id ON trade_log(position_id);

CREATE TABLE historical_outcomes (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    polymarket_id   TEXT NOT NULL,
    question        TEXT NOT NULL,
    question_embedding vector(1024),
    outcome         TEXT,
    final_price     DECIMAL,
    resolution_date TIMESTAMPTZ,
    metadata        JSONB DEFAULT '{}'
);

CREATE INDEX idx_markets_embedding ON markets USING hnsw (question_embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX idx_thesis_embedding ON thesis USING hnsw (thesis_embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX idx_historical_embedding ON historical_outcomes USING hnsw (question_embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

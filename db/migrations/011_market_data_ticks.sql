-- db/migrations/011_market_data_ticks.sql
-- Tick log for crypto venue market data: spot orderbook + trades from
-- Coinbase Advanced Trade, perp orderbook + trades + funding + mark/index
-- from Bybit. Feeds the next-generation predictor with features the
-- lognormal Phi(d2) estimator cannot see (depth imbalance, aggressor-side
-- flow, perp basis, funding pressure). Stored row-per-snapshot rather than
-- decision-time-only so analytics can replay any historical window.
--
-- Storage shape decisions:
--   * orderbook_snapshots: row every 1s per (venue, product). Top 10 levels
--     each side stored as JSONB plus pre-aggregated imbalance for fast
--     analytics that doesn't need to unpack JSON.
--   * trade_prints: row per fill. No aggregation; we want microstructure
--     fidelity (aggressor side, size distribution, burst patterns).
--   * funding_history: ~3 rows/day (Bybit funds every 8h). Trivial.
--   * mark_index: ~86k rows/day per perp product alongside orderbook.
--
-- Volumes at 1s cadence: ~200k rows/day across both venues' orderbooks
-- plus trades. Postgres handles 70M rows/year on a single node with the
-- BRIN(ts) indexes below; we can partition by month later if writes get
-- heavy.

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    venue           TEXT NOT NULL,
    product         TEXT NOT NULL,
    -- Top-of-book derived fields, stored explicitly so analytics queries
    -- avoid JSONB unpacking on every read.
    best_bid        NUMERIC,
    best_ask        NUMERIC,
    mid             NUMERIC,
    spread          NUMERIC,
    -- Top-N depth aggregates. depth_levels lets us count what's stored
    -- (we plan 10, but 25 or 50 may show up later from Bybit's deeper
    -- topic; the column documents the snapshot's depth).
    depth_levels    INTEGER NOT NULL DEFAULT 10,
    bid_volume_topN NUMERIC,
    ask_volume_topN NUMERIC,
    -- Pre-computed imbalance: (bid_vol - ask_vol) / (bid_vol + ask_vol).
    -- Range [-1, +1]; positive = bid-heavy. Materialized so range queries
    -- and the eventual learned model can read it without recomputing.
    imbalance       NUMERIC,
    -- Full top-N levels: [[price, size], ...] for each side. JSONB so we
    -- can index/extract specific levels later without schema changes.
    bids            JSONB,
    asks            JSONB
);

-- Time-range scans are the dominant access pattern. BRIN handles
-- monotonic timestamps in 0.1% the size of btree.
CREATE INDEX IF NOT EXISTS idx_orderbook_ts_brin
    ON orderbook_snapshots USING brin(ts);
-- Per-venue/product time queries (e.g. "give me Bybit BTCUSDT depth in
-- the last 5 min"). Btree because we filter on equality before range.
CREATE INDEX IF NOT EXISTS idx_orderbook_venue_product_ts
    ON orderbook_snapshots(venue, product, ts DESC);


CREATE TABLE IF NOT EXISTS trade_prints (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    venue           TEXT NOT NULL,
    product         TEXT NOT NULL,
    -- Aggressor side: the taker direction. 'buy' = buyer was the
    -- aggressor (lifted the offer); 'sell' = seller hit the bid.
    -- Coinbase reports this directly; Bybit reports a 'side' field
    -- with the same semantic.
    side            TEXT NOT NULL,
    price           NUMERIC NOT NULL,
    size            NUMERIC NOT NULL,
    -- Venue-assigned trade id when available, for dedup on reconnect.
    trade_id        TEXT
);

CREATE INDEX IF NOT EXISTS idx_trade_prints_ts_brin
    ON trade_prints USING brin(ts);
CREATE INDEX IF NOT EXISTS idx_trade_prints_venue_product_ts
    ON trade_prints(venue, product, ts DESC);
-- Dedup on reconnect: same (venue, trade_id) shouldn't insert twice.
CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_prints_venue_trade_id
    ON trade_prints(venue, trade_id) WHERE trade_id IS NOT NULL;


CREATE TABLE IF NOT EXISTS funding_history (
    ts              TIMESTAMPTZ NOT NULL,
    venue           TEXT NOT NULL,
    product         TEXT NOT NULL,
    -- Per-period funding rate (typically 8h). Positive = longs pay shorts.
    funding_rate    NUMERIC NOT NULL,
    -- Annualised equivalent for ranking/regime analysis.
    annualised_rate NUMERIC,
    PRIMARY KEY (ts, venue, product)
);

CREATE INDEX IF NOT EXISTS idx_funding_venue_product_ts
    ON funding_history(venue, product, ts DESC);


CREATE TABLE IF NOT EXISTS mark_index_prices (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    venue           TEXT NOT NULL,
    product         TEXT NOT NULL,
    mark_price      NUMERIC,
    index_price     NUMERIC,
    last_price      NUMERIC,
    -- Cross-asset spillover signal: perp_mark - spot_index. Positive
    -- basis means perps are pricing in upward pressure (or short squeeze
    -- carry), negative means contango is inverted. Stored materialized
    -- so a future learned model can read it directly without join.
    basis           NUMERIC
);

CREATE INDEX IF NOT EXISTS idx_mark_index_ts_brin
    ON mark_index_prices USING brin(ts);
CREATE INDEX IF NOT EXISTS idx_mark_index_venue_product_ts
    ON mark_index_prices(venue, product, ts DESC);

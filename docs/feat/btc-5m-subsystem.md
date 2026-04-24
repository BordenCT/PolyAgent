# BTC Short-Horizon Up/Down Subsystem (name: `btc5m`)

## Motivation

Polymarket runs continuous streams of short-horizon up/down binary markets
on BTC at multiple timeframes — confirmed **5-minute and 15-minute**
windows, with a slug convention (`btc-updown-Xm|Xh|Xd-<unix_ts>`) that is
ready to absorb hourly or daily expansions without code changes. Sample
market: "Bitcoin Up or Down - April 23, 9:50PM-9:55PM ET" with $8.7k
liquidity, 1¢ spread, $64k 24h volume.

These are the "faster markets" the main bot can't exploit with its 4-hour
scan cadence and LLM brain.

The existing PolyAgent pipeline (scan → brain → consensus → execute) is
designed for slow, qualitative markets where an LLM adds value. For BTC
up/down binaries the right decision is a lognormal calculation against a
live spot price, not LLM reasoning. We add a parallel subsystem that
bypasses the brain entirely and runs on a cadence appropriate for its
instrument.

**Naming note:** the subsystem retains the `btc5m` prefix in code
(tables, modules, env vars) as historical shorthand — it was designed for
5-minute markets first. Functionally it handles any `btc-updown-<duration>-<ts>`
slug; the `window_duration_s` column on each row distinguishes 5m from 15m
and from future timeframes. Renaming the code is deferred YAGNI.

This spec covers **Phase 1 (MVP, paper-only)** which delivers:
- Instrumentation to observe whether a math estimator has edge over the
  market mid after spreads and fees.
- An online backtest (paper-trading running forward) that accumulates data
  in weeks, not months.

**Phase 2 (live trading)** is deliberately deferred. If Phase 1's paper
trades show durable positive edge over 2–4 weeks, we flip a flag. If they
don't, we stop — and the project cost is one week of infrastructure, not
the six-week OddArb trap of building everything before measuring anything.

## Background: what we learned from OddArbTrading

A previous project (`~/Development/OddArbTrading`) attacked the same class
of markets on Kalshi and never went live across a 12-month development
window. Lessons we are explicitly applying:

**Port from OddArb:**
- Black-Scholes Φ(d₂) probability estimator (`trading/crypto/probability.py`).
- Multi-source price feed pattern (with a single clean source for BTC-only).
- Paper-trade tracking schema + feedback loop.
- Async orchestrator pattern adapted to PolyAgent's thread model.

**Avoid from OddArb:**
- No LLM-based market matching (half-removed dead code in OddArb).
- No ML trend classifier (the RandomForest model was trained on 1h sports
  markets and could not transfer; explicitly no ML in Phase 1).
- No monitoring for features that don't exist (OddArb built trade monitors
  for trades that were never placed).
- No mixed async/sync DB clients — one pattern, consistent with PolyAgent.
- No scope creep across assets or timeframes until BTC 5m pays its way.

## Background: what we learned from Chainlink research

Polymarket settles 5m BTC markets against the Chainlink BTC/USD **Data
Streams** oracle (`https://data.chain.link/streams/btc-usd`), specifically
not against exchange spot. Three hard facts shape the design:

1. **Data Streams access is gated.** Non-partners must request
   credentials via Typeform. Polymarket offers a sponsored Chainlink key
   program for developers building on their markets; this is the only
   realistic path for an indie developer.
2. **No historical archive exists** for Data Streams. Chainlink doesn't
   publish one and no third-party mirror was found. Legacy on-chain Price
   Feeds have a 1-hour heartbeat, which is too sparse to reconstruct a 5m
   settlement tick.
3. **Coinbase spot tracks Chainlink DON within a few bps.** Chainlink's
   CEX-aggregate reference pulls from Coinbase + Binance + others, so
   Coinbase USD spot is a close (but not identical) proxy.

Consequences:
- **Phase 1 uses Coinbase as the price source**, with the known basis
  risk. The estimator input is Coinbase, but settlement is Chainlink.
- **A background track applies for Polymarket's sponsored Chainlink key**
  now. When approved, we swap the price source with a config change — the
  `PriceSource` interface is designed for this swap.
- **Historical backtest is impossible** because Polymarket also purges
  resolved 5m markets from Gamma shortly after resolution (verified: a
  market that existed 4h ago returns empty now). The only path is a
  forward-running paper trader that captures data as it happens.

## Non-goals (explicit)

- Not covering ETH, SOL, XRP, DOGE. BTC only. Expansion happens across
  **timeframes** — 5m and 15m are in scope for Phase 1; 1h / 4h / 1d come
  for free with the same code the moment Polymarket lists them (the
  scanner regex and the estimator's TTM parameter already handle them).
- No live trading in Phase 1. Paper only.
- No LLM brain integration. Math estimator only.
- No orderbook microstructure modeling (imbalance, hidden liquidity).
  We accept the quoted mid as the reference price.
- No WebSocket infrastructure. REST polling for both spot and markets.
- No ML. No trend classifier. No reinforcement learning.
- No per-trade Kelly sizing in Phase 1 — fixed notional per trade.
  Kelly comes in Phase 2 once we have realized-edge data.

## Architecture

The subsystem lives under `polyagent/services/btc5m/` and registers one
new worker thread in `main.py` alongside the four existing workers
(scanner, brain, executor, exit_monitor). It does not touch any existing
service.

```
┌──────────────────────────────────────────────────────────┐
│  btc5m_worker (new thread in main.py)                    │
│  ──────────────────────────────────────────────────────  │
│                                                          │
│    spot_source      ─ every 2s ─→  spot_cache            │
│       │                                │                 │
│       │                                ▼                 │
│       │                          realized_vol            │
│       │                                │                 │
│    market_scanner ─ every 60s ─→  market_registry        │
│       │                                │                 │
│       └──────────┬─────────────────────┘                 │
│                  ▼                                       │
│              decider         ─→  paper_executor          │
│              (per active          ↓                      │
│              market, every    btc5m_trades table         │
│              60s)                                        │
│                                                          │
│              resolver        ─→  btc5m_markets update    │
│              (when market     (outcome + end_spot)       │
│              window ends)                                │
└──────────────────────────────────────────────────────────┘
```

Key design properties:
- **Single-threaded worker.** One loop handles everything. No cross-thread
  coordination. Simpler than OddArb's multi-collector model and adequate
  given sub-second latency is not required for 5m markets.
- **Spot polling at 2s.** Gives ~150 ticks per 5m window — enough for a
  meaningful realized-vol estimate. Can move to WebSocket in Phase 2 if
  evidence shows latency matters.
- **Market scanning at 60s.** New 5m markets spawn every 5 min, new 15m
  markets every 15 min; a 60s poll catches them within 1 minute of
  creation. Plenty of headroom because the trading window is ~24h before
  the resolution window.
- **Per-market decision at 60s.** Each active market is evaluated once a
  minute against the current estimator output. If the absolute edge
  exceeds threshold, record a paper trade.
- **Bypasses brain, strategies, kelly sizing.** All of those are wired
  for slow qualitative markets and contribute noise here.

## Components

### 1. `btc5m/spot.py` — Coinbase price source

Single module, single responsibility: maintain a rolling cache of BTC/USD
mid prices from Coinbase Advanced Trade, plus on-demand realized
volatility.

**Public interface:**

```python
class BtcSpotSource:
    def __init__(self, poll_interval_s: float = 2.0): ...
    def tick(self) -> Decimal: ...              # fetch one spot price and append
    def current(self) -> Decimal | None: ...    # last cached price
    def realized_vol(self, window_s: int = 300) -> float: ...
                                                # annualised σ over trailing window
```

- Uses `httpx` (already a dependency) against Coinbase's public REST:
  `https://api.exchange.coinbase.com/products/BTC-USD/ticker`.
- Ring buffer of (ts, price) pairs capped at 1 hour of ticks — more than
  enough for any realized-vol window we'd use.
- No persistence — cache is in-memory. Spot history persistence is a
  Phase 2 extension if we want cross-restart vol continuity.
- Returns `None` from `current()` only if the process hasn't ticked yet.
- The class is a **PriceSource interface stub**: when Chainlink Data
  Streams credentials arrive, a `ChainlinkSpotSource` subclass implements
  the same three methods. No other component changes.

### 2. `btc5m/estimator.py` — lognormal probability

Pure function. No I/O. Deterministic. Parameterized on time-to-maturity so
the same code serves 5m today and 1h/1d if expanded later.

```python
def estimate_up_probability(
    start_price: Decimal,
    current_spot: Decimal,
    seconds_to_resolution: float,
    annualised_vol: float,
) -> float:
    """P(spot_at_resolution >= start_price) under lognormal assumption.

    Uses Black-Scholes Φ(d₂):
        d₂ = (ln(S/K) + (r - σ²/2) * T) / (σ √T)
        P(up) = Φ(d₂)   where r = 0 for a fair game over 5 min

    For our market, K == start_price and S == current_spot.
    """
```

- Ported directly from OddArb's `probability.py` but stripped of the
  multi-asset handling.
- Unit-tested against known lognormal closed-form values (e.g., S=K,
  T=5min → probability ≈ 0.5 regardless of vol; S > K → probability > 0.5
  by known amount).
- Pure function, so no mocking overhead in tests.

### 3. `btc5m/scanner.py` — market discovery (any BTC up/down timeframe)

Polls Polymarket Gamma every 60s for markets whose slug matches
`btc-updown-<duration>-<unix_ts>` where `<duration>` is any
`\d+[mhd]` token (e.g. `5m`, `15m`, `1h`, `4h`, `1d`). For each:
- Parse `unix_ts` from slug → resolution time (window end).
- Parse `<duration>` → convert to seconds (`5m` → 300, `15m` → 900, `1h`
  → 3600, etc.). That becomes `window_duration_s`.
- `window_start_ts = window_end_ts - window_duration_s`.
- Upsert into `btc5m_markets` table keyed by polymarket_id, storing
  `window_duration_s` as a column.
- Skip markets we've already seen (idempotent).

```python
def scan(self, poll_interval_s: int = 60) -> list[Btc5mMarket]: ...
```

Returns the freshly-discovered markets so the worker loop can react.

### 4. `btc5m/book.py` — CLOB orderbook fetcher

Thin wrapper around Polymarket's existing CLOB client (already in the
codebase at `polyagent/data/clients/polymarket.py`). Given a token_id,
returns current best bid, best ask, mid. No new HTTP code — just reuses
the established `PolymarketClient` methods.

### 5. `btc5m/decider.py` — decision engine

For each active market in the registry, every 60s:

```
spot = spot_source.current()
vol  = spot_source.realized_vol(window_s=300)
ttm  = market.window_end_ts - now
p_up = estimator.estimate_up_probability(
           market.start_price or spot,   # use current spot if window hasn't opened
           spot,
           ttm,
           vol,
       )
book = book.fetch(market.token_id_yes)
mid  = (book.best_bid + book.best_ask) / 2

edge = p_up - mid
if abs(edge) > THRESHOLD and abs(edge) * size > FEES_PLUS_SPREAD:
    record_paper_trade(side=YES if edge > 0 else NO, size=..., ...)
```

- `THRESHOLD` is configurable (env var, default 0.05 — will recalibrate
  once we have paper-trade data).
- Fees + spread are modeled explicitly: we won't record a paper trade
  just because the raw edge is positive — the trade has to have positive
  expected value after realistic costs.
- One paper trade per market maximum (no re-entry). Keeps analysis clean.

### 6. `btc5m/executor.py` — paper executor

Writes `btc5m_trades` rows with the decision snapshot:

```
(market_id, decision_ts, side, fill_price_assumed, size,
 estimator_p_up, spot_at_decision, vol_at_decision, edge_at_decision)
```

`fill_price_assumed` is the ask (for YES buys) or the bid (for NO buys)
to model worst-case spread — no assumption that we'd ever cross the
spread more favourably.

### 7. `btc5m/resolver.py` — settlement recording

When a market's `window_end_ts` has passed:
1. Query our spot cache / Coinbase for the price at `window_end_ts` and
   `window_start_ts`. Both may be re-fetched via Coinbase's 1-minute
   candles endpoint for reliability.
2. Update `btc5m_markets`: set `start_spot`, `end_spot`, `outcome`
   (YES if end_spot >= start_spot, else NO).
3. For any paper trade on this market, compute realized PnL:
   - YES position: `size * (1.0 - fill_price)` if outcome YES else `-size * fill_price`.
   - NO position: `size * (fill_price)` if outcome NO else `-size * (1.0 - fill_price)`.
4. Update the `btc5m_trades.pnl` column.

Note: **the settlement outcome we record is the Coinbase-derived one, not
Chainlink's.** The basis between the two is tracked as a separate
metric (see below). Over enough trades, persistent basis will show as
systematic PnL drift, which is the signal to migrate to Chainlink.

### 8. `polyagent btc5m-stats` CLI

New Click command. One SQL join, rendered with Rich.

```
         BTC 5m Paper-Trading Performance (last 30 days)
 Trades  Wins  Losses  Win%  Avg Edge  Avg PnL  Total PnL  Realized Vol (avg)
 412     212   200     51%   +0.031    +$0.08   +$32.96    0.48
```

Key columns:
- `Avg Edge`: average of `estimator_p_up - market_mid` at decision time.
- `Avg PnL`: per-trade mean realized PnL.
- `Total PnL`: sum over all trades in the window.
- `Realized Vol`: average realized vol at decision time (sanity check on
  the input).

Slicing options via flags: `--by-day`, `--by-duration` (5m vs 15m vs future),
`--by-ttm-bucket` (TTM at decision), `--by-edge-bucket`, `--last N`.

## Data Model

### Migration `005_btc5m.sql`

```sql
CREATE TABLE btc5m_markets (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    polymarket_id    TEXT UNIQUE NOT NULL,
    slug             TEXT UNIQUE NOT NULL,
    token_id_yes     TEXT NOT NULL,
    token_id_no      TEXT NOT NULL,
    window_duration_s INTEGER NOT NULL,       -- 300 for 5m, 900 for 15m, 3600 for 1h, etc.
    window_start_ts  TIMESTAMPTZ NOT NULL,    -- derived from slug
    window_end_ts    TIMESTAMPTZ NOT NULL,    -- derived from slug
    start_spot       DECIMAL,                 -- Coinbase spot at window_start_ts, set on resolve
    end_spot         DECIMAL,                 -- Coinbase spot at window_end_ts, set on resolve
    outcome          TEXT,                    -- 'YES'|'NO', set on resolve
    discovered_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at      TIMESTAMPTZ
);
CREATE INDEX idx_btc5m_markets_window_end ON btc5m_markets(window_end_ts);
CREATE INDEX idx_btc5m_markets_outcome    ON btc5m_markets(outcome);
CREATE INDEX idx_btc5m_markets_duration   ON btc5m_markets(window_duration_s);

CREATE TABLE btc5m_trades (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    market_id           UUID NOT NULL REFERENCES btc5m_markets(id),
    decision_ts         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    side                TEXT NOT NULL,               -- 'YES' | 'NO'
    fill_price_assumed  DECIMAL NOT NULL,
    size                DECIMAL NOT NULL,
    estimator_p_up      DECIMAL NOT NULL,
    spot_at_decision    DECIMAL NOT NULL,
    vol_at_decision     DECIMAL NOT NULL,
    edge_at_decision    DECIMAL NOT NULL,
    pnl                 DECIMAL,                     -- set on resolution
    resolved_at         TIMESTAMPTZ
);
CREATE INDEX idx_btc5m_trades_market    ON btc5m_trades(market_id);
CREATE INDEX idx_btc5m_trades_decision  ON btc5m_trades(decision_ts DESC);
```

No `btc5m_spot_ticks` table in Phase 1 — the cache is in-memory and
reconstructed from Coinbase candles on demand. Persistence is a Phase 2
concern when we need cross-restart continuity.

## Configuration

New env vars in `Settings`:

| Variable | Default | Meaning |
|---|---|---|
| `BTC5M_ENABLED` | `false` | Master switch for the subsystem |
| `BTC5M_SPOT_POLL_S` | `2` | Coinbase spot poll interval (seconds) |
| `BTC5M_MARKET_POLL_S` | `60` | Gamma market-scan interval |
| `BTC5M_VOL_WINDOW_S` | `300` | Rolling window for realized vol |
| `BTC5M_EDGE_THRESHOLD` | `0.05` | Min absolute edge to trigger paper trade |
| `BTC5M_POSITION_SIZE_USD` | `5.00` | Fixed notional per paper trade |
| `BTC5M_FEES_BPS` | `0` | Assumed round-trip fees (Polymarket is currently 0) |

Phase 2 adds `BTC5M_LIVE_ENABLED` (separate from the existing
`POLYMARKET_LIVE_ENABLED` so the two bots' live state is independent) and
`BTC5M_BANKROLL_USD`.

## Error Handling

- Coinbase REST timeout / 500 → skip tick, log warning, don't crash. A
  tick gap > 30s triggers a louder warning (the vol estimate degrades).
- Polymarket Gamma timeout → skip this poll, retry next interval.
- Estimator division by zero (e.g., vol = 0) → return 0.5 and log once
  per market. This happens only if we have zero price movement over the
  vol window, which is vanishingly unlikely for BTC.
- Market slug parse failure → skip and log.
- DB write failure → log and continue. The decision loop must be robust
  to transient DB errors because losing one minute's trade is fine.

## Testing

- **Unit tests (`tests/unit/test_btc5m_*.py`)**
  - Estimator: 8+ test cases covering ATM (S=K), deep ITM, deep OTM,
    near-expiration, high/low vol.
  - Slug parser: valid slugs, malformed slugs, wrong-asset slugs.
  - Realized vol computation: synthetic tick stream with known σ.
  - Paper executor: mock DB, confirm correct row shape and fill-price
    selection based on side.

- **Integration tests (`tests/integration/test_btc5m_*.py`)**
  - Scanner against a mocked Gamma response: discover 3 markets, upsert
    exactly those rows.
  - Resolver: insert a fake market with a past window_end_ts, mock
    Coinbase candles, run resolver, assert outcome + PnL updates.
  - End-to-end: seed a market, tick spot through the window, run decider
    and resolver, assert a paper trade exists with PnL.
  - CLI `btc5m-stats` with seeded trades across multiple outcomes.

Tests follow PolyAgent's existing pattern (pytest, `--run-integration`
flag, `settings` fixture pointing to `polyagent_test`).

## Rollout

Phase 1 implementation order, each task is a separate commit:

1. Schema migration `005_btc5m.sql`.
2. Domain models (`Btc5mMarket`, `Btc5mTrade` dataclasses in `models.py`).
3. `spot.py` + unit tests.
4. `estimator.py` + unit tests.
5. `scanner.py` + slug parser + unit tests.
6. Repository layer (CRUD for both tables).
7. `decider.py` + `executor.py` + unit tests.
8. `resolver.py` + integration test.
9. Worker loop in `main.py` (behind `BTC5M_ENABLED` flag).
10. `polyagent btc5m-stats` CLI + integration test.

After landing, enable in the config (`BTC5M_ENABLED=true`) and let it
run. At 2 weeks, run `polyagent btc5m-stats --by-edge-bucket` and
evaluate whether edge survives costs.

## Decision gate for Phase 2

Phase 2 (live trading) is authorized if **all three** are true at the
2–4 week mark:

1. **Total P&L ≥ $5.** Not "positive" — meaningfully positive after
   fees/spread. Small-sample positive may be luck.
2. **Win rate on above-threshold trades ≥ 52%** (breakeven on a 1¢
   spread is ~50.5%; 52% has a buffer).
3. **No systematic basis drift.** The Coinbase→Chainlink basis, computed
   from resolved markets where our recorded `outcome` disagreed with
   Polymarket's actual resolution, is ≤ 1 trade per 100.

Failing any of these, we stop. The infrastructure is cheap to keep (it
costs only the Coinbase API polls), so we can leave it running while we
wait for Chainlink credentials to arrive and re-run the test with a
better price source.

## Future work (out of scope for this spec)

- **Phase 2: live trading.** Flip paper → live behind the new env var.
  Per-trade log already exists. Needs bankroll cap, per-day loss cap,
  Kelly sizing option.
- **Chainlink Data Streams migration.** When credentials arrive, swap
  `BtcSpotSource` for `ChainlinkSpotSource` via config; no other change.
- **Timeframe expansion beyond 5m/15m.** The scanner regex and estimator
  TTM input already accept any duration; when Polymarket lists 1h/4h/1d
  BTC up/down binaries, they are picked up for free.
- **WebSocket spot feed.** Only if latency ends up mattering.
- **Kelly sizing.** Only after we have realized-edge data to fit Kelly's
  underlying parameters.
- **Cross-market arbitrage.** If 5m and 1h markets both exist on BTC,
  their prices imply a term structure that can be arbed.

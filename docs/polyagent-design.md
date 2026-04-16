# PolyAgent Design Spec

**Date:** 2026-04-15
**Status:** Draft
**Author:** Charles Borden

---

## Overview

PolyAgent is an autonomous Polymarket prediction market trading bot. It scans live markets, evaluates opportunities using Claude as its analytical brain, sizes positions with Kelly Criterion, executes trades via consensus of three strategy agents, and manages exits with a 3-trigger system.

The bot runs in paper trading mode by default. A single config flag enables live execution.

### Goals

- Scan 500+ active markets per cycle, filter to high-EV opportunities
- Generate probability estimates and theses via Claude API
- Size positions with Kelly Criterion (quarter-kelly cap)
- Execute trades only when 2/3 strategy agents agree
- Exit positions before settlement using volume/target/decay triggers
- Store all state in PostgreSQL for auditability
- Use pgvector for RAG-enhanced analysis and market similarity
- Provide CLI-first observability into bot performance

---

## Architecture

### Runtime

- **Python 3.14** free-threaded build (`--disable-gil`) for true thread parallelism
- **Rust** polymarket-cli as subprocess for supplemental market commands
- **Podman** containerized: 2 containers (app + db)

### Containers

| Container | Image | Purpose |
|-----------|-------|---------|
| `polyagent-app` | Python 3.14t + Rust polymarket-cli | Bot + CLI |
| `polyagent-db` | PostgreSQL 17 + pgvector | All persistent state |

### Thread Pool

Dynamic `WorkerPool` that auto-sizes based on `os.cpu_count()` with env var overrides.

**Target environment:** MS-A2 with 96-thread AMD CPU.

| Component | Default Workers | Scaling | Bottleneck |
|-----------|----------------|---------|------------|
| Scanner | `max(1, cores // 3)` | I/O bound (API calls) | Polymarket API rate limits |
| Brain | `max(1, cores // 6)` | API rate-limited | Anthropic RPM/TPM limits |
| Executor | `max(1, cores // 24)` | Logical constraint | Consensus serialization per-market |
| Exit Monitor | `max(1, cores // 6)` | Polling frequency | Open position count |

Override via env vars: `SCANNER_WORKERS=32`, `BRAIN_WORKERS=16`, etc.

### Inter-Thread Communication

`queue.Queue` instances (thread-safe) as the pipeline:

```
Scanner workers --> [scan_queue] --> Brain workers --> [thesis_queue] --> Executor workers
                                                                              |
                                                                              v
                                                              Exit Monitor workers
                                                              (polls positions table)
```

No file-based IPC. All intermediate state persisted to PostgreSQL.

---

## Data Model

### PostgreSQL 17 + pgvector

#### markets

| Column | Type | Notes |
|--------|------|-------|
| id | UUID | PK |
| polymarket_id | TEXT | Unique, from Polymarket API |
| question | TEXT | Market question text |
| question_embedding | vector(1024) | For similarity search |
| category | TEXT | crypto, politics, macro, etc. |
| token_id | TEXT | CLOB token identifier |
| midpoint_price | DECIMAL | Current midpoint |
| bids_depth | DECIMAL | Buy-side depth |
| asks_depth | DECIMAL | Sell-side depth |
| hours_to_resolution | DECIMAL | Time remaining |
| volume_24h | DECIMAL | 24h trading volume |
| scanned_at | TIMESTAMPTZ | When scanned |
| score | JSONB | `{gap, depth, hours, ev}` |
| status | TEXT | queued, evaluating, rejected, traded |

#### target_wallets

| Column | Type | Notes |
|--------|------|-------|
| id | UUID | PK |
| address | TEXT | Unique wallet address |
| total_trades | INT | Historical trade count |
| win_rate | DECIMAL | Historical win rate |
| total_pnl | DECIMAL | Historical P&L |
| wallet_embedding | vector(1024) | Trading pattern similarity |
| discovered_at | TIMESTAMPTZ | When identified |

#### thesis

| Column | Type | Notes |
|--------|------|-------|
| id | UUID | PK |
| market_id | UUID | FK to markets |
| thesis_embedding | vector(1024) | For dedup/similarity |
| claude_estimate | DECIMAL | Probability estimate (0-1) |
| confidence | DECIMAL | Thesis confidence (0-1) |
| checks | JSONB | `{base_rate, news, whale, disposition}` |
| checks_passed | INT | Count of passing checks (0-4) |
| thesis_text | TEXT | Claude's full analysis |
| strategy_votes | JSONB | `{arbitrage: "BUY", convergence: "HOLD", ...}` |
| consensus | TEXT | full, half, none |
| created_at | TIMESTAMPTZ | When generated |

#### positions

| Column | Type | Notes |
|--------|------|-------|
| id | UUID | PK |
| thesis_id | UUID | FK to thesis |
| market_id | UUID | FK to markets |
| side | TEXT | BUY or SELL |
| entry_price | DECIMAL | Price at entry |
| target_price | DECIMAL | Expected exit price |
| kelly_fraction | DECIMAL | Kelly f* value |
| position_size | DECIMAL | Dollar size |
| current_price | DECIMAL | Latest price |
| status | TEXT | open, closed |
| exit_reason | TEXT | TARGET_HIT, VOLUME_EXIT, STALE_THESIS |
| pnl | DECIMAL | Realized P&L |
| paper_trade | BOOLEAN | true until live mode |
| opened_at | TIMESTAMPTZ | When opened |
| closed_at | TIMESTAMPTZ | When closed |

#### trade_log

| Column | Type | Notes |
|--------|------|-------|
| id | UUID | PK |
| position_id | UUID | FK to positions |
| action | TEXT | OPEN, CLOSE, SKIP |
| reason | TEXT | Human-readable reason |
| raw_request | JSONB | API request payload |
| raw_response | JSONB | API response payload |
| logged_at | TIMESTAMPTZ | When logged |

#### historical_outcomes

| Column | Type | Notes |
|--------|------|-------|
| id | UUID | PK |
| polymarket_id | TEXT | Original market ID |
| question | TEXT | Market question |
| question_embedding | vector(1024) | For RAG base rate lookup |
| outcome | TEXT | Resolution outcome |
| final_price | DECIMAL | Final settlement price |
| resolution_date | TIMESTAMPTZ | When resolved |
| metadata | JSONB | Wallet activity, volume patterns |

#### pgvector Indexes

- HNSW on `markets.question_embedding`
- HNSW on `thesis.thesis_embedding`
- HNSW on `historical_outcomes.question_embedding`

---

## Application Layers

```
polyagent/
├── cli/                        # Click-based CLI
│   ├── __init__.py
│   ├── main.py                 # Click group entry point
│   ├── status.py               # Live bot state
│   ├── positions.py            # Position management
│   ├── performance.py          # P&L analytics
│   └── markets.py              # Market inspection + thesis lookup
├── api/                        # Health/metrics (FastAPI)
│   ├── health.py               # /health, /ready
│   └── metrics.py              # Position stats, win rate
├── services/                   # Domain logic (no HTTP, no SQL)
│   ├── scanner.py              # Market scoring
│   ├── brain.py                # 4-check evaluation, thesis generation
│   ├── executor.py             # Consensus voting, Kelly sizing
│   ├── exit_monitor.py         # 3-trigger exit logic
│   └── embeddings.py           # Embedding generation + similarity
├── strategies/                 # Pluggable strategy agents
│   ├── base.py                 # Strategy interface
│   ├── arbitrage.py            # Price gaps between related markets
│   ├── convergence.py          # Enter when price moves toward estimate
│   └── whale_copy.py           # Mirror target wallets with delay
├── data/                       # Data access layer
│   ├── repositories/           # DB queries per entity
│   │   ├── markets.py
│   │   ├── thesis.py
│   │   ├── positions.py
│   │   ├── wallets.py
│   │   └── historical.py
│   └── clients/                # External API clients
│       ├── polymarket.py       # py-clob-client + CLI subprocess
│       └── claude.py           # Anthropic SDK with prompt caching
├── infra/                      # Infrastructure wiring
│   ├── config.py               # Env vars, settings
│   ├── database.py             # psycopg connection pool
│   ├── pool.py                 # Dynamic WorkerPool
│   ├── queues.py               # Inter-thread queue definitions
│   └── logging.py              # Structured JSON logging
├── scripts/
│   ├── analyze_wallets.py      # One-time: poly_data -> target_wallets
│   └── backfill_embeddings.py  # One-time: embed historical outcomes
├── main.py                     # Entry point
├── Containerfile
├── compose.yaml
└── pyproject.toml
```

### Layer Rules

- **Services** receive data, return decisions. No HTTP, no SQL, no side effects. Dependencies injected.
- **Strategies** implement `evaluate(market) -> Vote` interface. Adding a strategy = one file.
- **Repositories** handle all database access. Services never touch SQL directly.
- **Clients** wrap external APIs. Services never make HTTP calls directly.

---

## Core Algorithms

### Scanner: Market Scoring

The article calls `claude_estimate(m)` for all 500 markets, but that's 500 API calls per cycle just for scanning. Instead, we use pgvector: embed the market question, retrieve similar resolved historical outcomes, and use their outcome distribution as the initial probability estimate. This eliminates Claude from the scan loop entirely — Claude is reserved for the brain's deep 4-check evaluation on survivors only.

```python
def score_market(market, historical_estimate):
    # historical_estimate from pgvector similarity lookup
    gap = abs(historical_estimate - market.midpoint_price)
    depth = min(market.bids_depth, market.asks_depth)
    hours_left = market.hours_to_resolution

    # Kill filters
    if gap < 0.07:   return None  # edge too thin
    if depth < 500:  return None  # can't fill
    if hours_left < 4:   return None  # too late
    if hours_left > 168: return None  # too slow

    return Score(
        gap=round(gap, 3),
        depth=depth,
        hours=hours_left,
        ev=round(gap * depth * 0.001, 2)
    )
```

~93% of markets filtered out. That's the point.

### Brain: 4-Check Evaluation

For every market in the queue, Claude runs 4 checks:

1. **Base rate** — RAG lookup: retrieve 10 most similar resolved markets from pgvector, calculate historical outcome distribution
2. **News** — Has anything changed in last 6h that affects this market?
3. **Whale check** — Are any of the target wallets active in this market?
4. **Disposition** — Is the crowd making a cognitive error (anchoring, recency bias)?

Decision logic:
- 3/4 checks agree -> generate thesis
- Thesis confidence > 75% -> size with Kelly
- Kelly says overbet -> cut to quarter Kelly

### Kelly Criterion Sizing

```python
def kelly_size(p_win, market_price, bankroll, max_fraction=0.25):
    b = (1 / market_price) - 1   # payout ratio
    q = 1 - p_win                 # loss probability
    f_star = (p_win * b - q) / b  # optimal fraction

    if f_star <= 0:
        return 0  # negative EV, kill trade

    f_capped = min(f_star, max_fraction)
    return round(bankroll * f_capped, 2)
```

- f* < 0: negative EV, skip
- f* > 0.25: overbetting, cap at quarter Kelly
- Sweet spot: f* between 0.05 and 0.15

### Consensus Voting

```python
def execute_consensus(agents, market, bankroll):
    votes = [agent.evaluate(market) for agent in agents]
    buy_votes = sum(1 for v in votes if v.action == "BUY")

    if buy_votes >= 2:  # full position
        size = kelly_size(...)
    elif buy_votes == 1:  # half position
        size = kelly_size(...) * 0.5
    else:  # no trade
        return None
```

### Exit Triggers

Three independent triggers, any one fires an exit:

1. **Target hit** — 85% of expected move captured
   ```python
   if current_price >= entry_price + (expected_gap * 0.85):
       exit("TARGET_HIT")
   ```

2. **Volume spike** — 3x normal volume = smart money leaving
   ```python
   if volume_10min > avg_volume_10min * 3:
       exit("VOLUME_EXIT")
   ```

3. **Time decay** — Thesis stale after 24h with < 2% price movement
   ```python
   if hours_since_entry > 24 and abs(price_change) < 0.02:
       exit("STALE_THESIS")
   ```

---

## Embeddings & RAG

### Provider

Voyage AI `voyage-3.5-lite` (1024 dimensions):
- $0.02/MTok
- Free tier: 200M tokens/month (more than sufficient)
- Falls within free tier for our usage

### pgvector Usage

**Market similarity (arbitrage detection):**
Query `markets.question_embedding` to find semantically related markets with price discrepancies.

**RAG for brain (base rate analysis):**
Query `historical_outcomes.question_embedding` for the 10 most similar resolved markets. Feed outcomes + context to Claude as part of the base rate check.

**Thesis dedup:**
Before evaluating a market, check `thesis.thesis_embedding` for a recent similar thesis (cosine similarity > 0.95, created within 6 hours). If found, reuse it and skip the Claude call.

---

## CLI

Entry points:

```toml
[project.scripts]
polyagent = "polyagent.cli.main:cli"
polyagent-bot = "polyagent.main:run"
```

### Commands

```
polyagent --help                  # Top-level help with all commands

polyagent status                  # Workers, queue depths, uptime
polyagent status --watch          # Auto-refresh every 5s

polyagent perf                    # Total P&L, win rate, Sharpe, trade count
polyagent perf --daily            # Day-by-day breakdown
polyagent perf --by-strategy      # Per-strategy performance
polyagent perf --by-category      # Per-category performance

polyagent positions               # Open positions with current P&L
polyagent positions --closed      # Historical with exit reasons
polyagent positions --worst       # Biggest losers

polyagent markets                 # Current queue with IDs and scores
polyagent markets --rejected      # Filtered markets and reasons

polyagent thesis <MARKET_ID>      # Full thesis + checks for a market
```

`polyagent markets` output includes market IDs for use with `polyagent thesis <MARKET_ID>`. Each command's `--help` explains flags and cross-references related commands.

---

## Cost Estimates

### Claude API (Sonnet 4)

**Per market evaluation:**

| Component | Tokens | Rate | Cost |
|-----------|--------|------|------|
| System prompt (cached) | ~1,500 | $0.30/MTok | $0.00045 |
| Market data + RAG context | ~2,500 | $3.00/MTok | $0.00750 |
| Output (thesis) | ~800 | $15.00/MTok | $0.01200 |
| **Total per market** | **~4,800** | | **~$0.02** |

**Per scan cycle (35 markets survive filtering):**

| Item | Cost |
|------|------|
| 35 market evaluations | $0.70 |
| Thesis dedup savings (~20% skip rate) | -$0.14 |
| **Net per cycle** | **~$0.56** |

### Scan Frequency Scenarios

| Frequency | Cycles/Day | Claude $/Day | Claude $/Month |
|-----------|-----------|-------------|---------------|
| Every hour | 24 | $13.44 | ~$403 |
| Every 4 hours | 6 | $3.36 | ~$101 |
| Every 6 hours | 4 | $2.24 | ~$67 |
| Daily (article's approach) | 1 | $0.56 | ~$17 |

**Recommended starting point:** Every 4 hours (6 cycles/day) — balances opportunity capture with cost. ~$101/month for Claude API.

### Embedding Costs (Voyage AI)

| Item | Tokens/Day (4hr cycle) | Cost |
|------|----------------------|------|
| Market question embeddings | ~21,000 (35 markets * 6 cycles * ~100 tok) | Free tier |
| RAG query embeddings | ~6,000 | Free tier |
| **Monthly total** | ~810,000 | **$0 (free tier)** |

Voyage AI free tier covers 200M tokens/month. Our usage (~810K/month) is < 0.5% of that.

### Data Ingestion

| Source | Per Cycle | Per Hour (4hr freq) | Per Day |
|--------|-----------|-------------------|---------|
| Market scan (500 markets) | ~1 MB | ~0.25 MB | ~6 MB |
| Order books (35 survivors) | ~175 KB | ~44 KB | ~1 MB |
| Historical poly_data | One-time: ~2-5 GB | — | — |
| **DB growth** | ~2 MB | ~0.5 MB | ~12 MB |

Monthly DB growth: ~360 MB (positions, theses, logs, embeddings).

### Infrastructure

| Resource | Cost |
|----------|------|
| MS-A2 (96-thread AMD) | Already provisioned |
| PostgreSQL 17 + pgvector | Runs on same machine (containerized) |
| Polymarket API | Free (read-only, no auth) |
| Voyage AI embeddings | Free tier |

### Total Monthly Cost Summary

| Scan Frequency | Claude API | Embeddings | Infra | Total |
|----------------|-----------|------------|-------|-------|
| Hourly | ~$403 | $0 | $0* | **~$403/mo** |
| Every 4 hours | ~$101 | $0 | $0* | **~$101/mo** |
| Every 6 hours | ~$67 | $0 | $0* | **~$67/mo** |
| Daily | ~$17 | $0 | $0* | **~$17/mo** |

*Infrastructure already provisioned on MS-A2.

---

## Configuration

All via environment variables:

```env
# Mode
PAPER_TRADE=true                    # false for live trading

# Scan
SCAN_INTERVAL_HOURS=4               # How often to run full scan cycle
SCAN_MARKET_LIMIT=500               # Markets to pull per scan

# Scoring thresholds
MIN_GAP=0.07                        # Minimum price-estimate gap
MIN_DEPTH=500                       # Minimum order book depth ($)
MIN_HOURS=4                         # Minimum hours to resolution
MAX_HOURS=168                       # Maximum hours to resolution

# Brain
ANTHROPIC_API_KEY=sk-...            # Claude API key
BRAIN_CONFIDENCE_THRESHOLD=0.75     # Minimum thesis confidence
BRAIN_MIN_CHECKS=3                  # Checks that must pass (out of 4)

# Kelly
KELLY_MAX_FRACTION=0.25             # Maximum Kelly fraction
BANKROLL=800                        # Starting bankroll ($)

# Exit
EXIT_TARGET_PCT=0.85                # Exit at 85% of expected move
EXIT_VOLUME_MULTIPLIER=3            # Volume spike multiplier
EXIT_STALE_HOURS=24                 # Hours before thesis considered stale
EXIT_STALE_THRESHOLD=0.02           # Min price change to not be stale

# Workers (override auto-scaling)
SCANNER_WORKERS=                    # Empty = auto-scale
BRAIN_WORKERS=
EXECUTOR_WORKERS=
EXIT_WORKERS=

# Database
DATABASE_URL=postgresql://polyagent:polyagent@polyagent-db:5432/polyagent

# Embeddings
VOYAGE_API_KEY=                     # Voyage AI key (optional, has free tier)

# Polymarket
POLYMARKET_API_URL=https://clob.polymarket.com
```

---

## Paper Trading Mode

When `PAPER_TRADE=true`:
- Scanner, Brain, and Executor run identically to live mode
- Executor logs the trade to `positions` and `trade_log` with `paper_trade=true`
- No actual orders placed via Polymarket API
- Exit Monitor tracks simulated positions using real market prices
- All CLI commands work identically — P&L reflects paper performance

Switching to live: set `PAPER_TRADE=false`, provide Polymarket wallet credentials.

---

## What's Excluded (for now)

- Frontend dashboard (CLI-first)
- Live trading execution (paper mode only in v1)
- Category rotation logic (manual for now)
- Polymarket wallet/signing setup
- Alerting/notifications (Telegram, Discord, etc.)

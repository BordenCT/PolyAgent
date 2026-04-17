# PolyAgent — Getting Started

## Prerequisites

- Podman + podman-compose
- Python 3.14 (installed via uv)
- Git
- Anthropic API key
- Ollama LXC at `192.168.1.56` with phi4:14b (scanner estimates)
- (Optional) Voyage AI API key for embeddings

---

## 1. Initial Setup

```bash
# Clone and enter project
cd ~/Development/PolyAgent

# Create virtual environment and install deps
uv venv --python 3.14
source .venv/bin/activate
uv pip install -e ".[dev]"

# Copy environment config
cp .env.example .env

# Edit .env — set your API keys
# REQUIRED: ANTHROPIC_API_KEY=sk-ant-your-key-here
# OPTIONAL: VOYAGE_API_KEY=your-voyage-key
# Ollama is pre-configured to 192.168.1.56 (phi4:14b)
vim .env

# Verify Ollama is reachable
curl -s http://192.168.1.56:11434/api/tags | python -m json.tool | head -5
# Should show phi4:14b in the model list

# Start the database
podman-compose up -d polyagent-db

# Verify database is healthy
podman ps --filter name=polyagent-db
# Should show: STATUS = Up ... (healthy)

# Run unit tests to verify everything works
.venv/bin/python -m pytest tests/ --tb=short -q
# Expected: 73 passed, 4 skipped
```

---

## 2. Backtesting

Validate the strategy against historical data before risking anything.

### 2a. Get Historical Data

```bash
# Download and process historical data (pure Python, no system deps needed)
# Downloads pre-built snapshot (~2GB), fetches market metadata, processes trades
polyagent ingest --snapshot

# Or specify a custom data directory
polyagent ingest --snapshot --data-dir ~/polyagent-data

# Alternative: full scrape from Goldsky subgraph (2+ days, but most complete)
# polyagent ingest --full
```

### 2b. Run Backtests

```bash
# Sanity check — midpoint estimator should produce ~$0 P&L (no edge)
polyagent backtest \
  --start 2025-01-01 \
  --end 2026-04-01 \
  --estimator midpoint \
  --data-dir ./data

# Theoretical ceiling — historical estimator uses perfect foresight
polyagent backtest \
  --start 2025-01-01 \
  --end 2026-04-01 \
  --estimator historical \
  --data-dir ./data

# Realistic test — Ollama phi4:14b estimates (free, uses actual LLM reasoning)
polyagent backtest \
  --start 2025-01-01 \
  --end 2026-04-01 \
  --estimator ollama \
  --data-dir ./data

# Compare all estimators side by side
polyagent backtest \
  --start 2025-01-01 \
  --end 2026-04-01 \
  --compare \
  --data-dir ./data

# Tune parameters — try different bankrolls and Kelly fractions
polyagent backtest \
  --start 2025-06-01 \
  --end 2026-04-01 \
  --estimator historical \
  --bankroll 2000 \
  --kelly-max 0.15 \
  --data-dir ./data
```

### 2c. Interpret Results

| Metric | Good Sign | Red Flag |
|--------|-----------|----------|
| Win Rate | > 60% | < 50% |
| Sharpe | > 1.5 | < 0.5 |
| Max Drawdown | < 15% | > 30% |
| Profit Factor | > 1.5 | < 1.0 |

If the backtest looks bad, tune thresholds in `.env` before proceeding:
- `MIN_GAP` — raise to be more selective (fewer but higher-quality trades)
- `MIN_DEPTH` — raise to avoid slippage in thin markets
- `KELLY_MAX_FRACTION` — lower to reduce position sizes
- `BRAIN_CONFIDENCE_THRESHOLD` — raise to require higher Claude confidence

---

## 3. Paper Trading

Run the full bot against live markets but without real money.

### 3a. Verify Config

```bash
# Confirm paper mode is on
grep PAPER_TRADE .env
# Should show: PAPER_TRADE=true

# Confirm scan frequency
grep SCAN_INTERVAL_HOURS .env
# Recommended start: 4 (every 4 hours, ~$101/mo Claude API)
# Conservative: 6 (every 6 hours, ~$67/mo)
```

### 3b. Start the Bot

```bash
# Option A: Run directly (foreground, see logs live)
source .venv/bin/activate
polyagent-bot

# Option B: Run in a screen/tmux session
screen -S polyagent
source .venv/bin/activate
polyagent-bot
# Detach: Ctrl+A, D
# Reattach: screen -r polyagent

# Option C: Run via container (after building)
podman-compose build polyagent-app
podman-compose up -d
```

### 3c. Monitor Performance

```bash
# Live status — workers, queue depths, market counts
polyagent status
polyagent status --watch  # auto-refresh every 5s

# Check current open positions
polyagent positions

# See overall P&L, win rate, Sharpe
polyagent perf

# Daily breakdown
polyagent perf --daily

# See what markets are in the queue
polyagent markets

# See rejected markets and why
polyagent markets --rejected

# Inspect a specific market's thesis
polyagent markets                    # grab an ID from the output
polyagent thesis a3f8c2d1            # first 8 chars of the UUID

# Check worst trades to learn from mistakes
polyagent positions --worst

# Check closed positions with exit reasons
polyagent positions --closed
```

### 3d. Paper Trading Checklist

Run paper trading for **at least 2 weeks** before going live. Check these:

- [ ] Bot runs stable for 48+ hours without crashes
- [ ] Win rate > 55% over 50+ trades
- [ ] Sharpe > 1.0
- [ ] Max drawdown < 20% of bankroll
- [ ] Exit triggers firing correctly (check `polyagent positions --closed`)
- [ ] No excessive API costs (check Anthropic dashboard)
- [ ] Scanner filtering rate ~90%+ (most markets killed, good)
- [ ] Brain rejection rate ~50%+ (quality gate working)

---

## 4. Live Trading

**Only proceed after paper trading validates the strategy.**

### 4a. Set Up Polymarket Wallet

```bash
# Install polymarket-cli (if not using container)
cargo install --git https://github.com/Polymarket/polymarket-cli --locked

# Create or import a wallet
polymarket wallet create
# OR
polymarket wallet import --private-key YOUR_PRIVATE_KEY

# Fund the wallet with USDC on Polygon
# Transfer USDC to your wallet address on Polygon network
polymarket wallet balance
```

### 4b. Switch to Live Mode

```bash
# Stop the bot
# If running in foreground: Ctrl+C
# If running in screen: screen -r polyagent, then Ctrl+C
# If running in container: podman-compose down polyagent-app

# Update .env
sed -i 's/PAPER_TRADE=true/PAPER_TRADE=false/' .env

# Set conservative bankroll — start small
sed -i 's/BANKROLL=800/BANKROLL=200/' .env

# Double-check settings
cat .env | grep -E "PAPER_TRADE|BANKROLL|KELLY_MAX"
# Should show:
#   PAPER_TRADE=false
#   BANKROLL=200
#   KELLY_MAX_FRACTION=0.25

# Start the bot
polyagent-bot
```

### 4c. Live Monitoring

```bash
# Same CLI commands work for live positions
polyagent status --watch
polyagent perf --daily
polyagent positions

# Watch logs for trade execution
# If running in container:
podman logs -f polyagent-app
```

### 4d. Live Trading Safety Checklist

- [ ] Start with $200-500 max (not your full target bankroll)
- [ ] Monitor every trade for the first 48 hours
- [ ] Verify exit triggers fire on real positions
- [ ] Check Polymarket wallet balance matches expected P&L
- [ ] Scale bankroll gradually: $200 -> $500 -> $1000 -> target
- [ ] Set up alerts (future: Telegram/Discord notifications)

### 4e. Emergency Stop

```bash
# Immediate shutdown
podman-compose down
# OR
pkill -f polyagent-bot

# The bot does NOT auto-close positions on shutdown.
# Open positions remain until manually closed or the market resolves.
# To close all positions manually:
polymarket positions list
polymarket sell --token-id TOKEN_ID --amount SIZE
```

---

## Cost Reference

Scanner estimates run on local Ollama (phi4:14b) at $0. Only the brain's Claude evaluations cost money.

| Scan Frequency | Scanner (Ollama) | Brain (Claude)/Month | Best For |
|---------------|-----------------|---------------------|----------|
| Every hour | $0 | ~$403 | Active markets, maximum opportunity capture |
| Every 4 hours | $0 | ~$101 | Balanced — recommended starting point |
| Every 6 hours | $0 | ~$67 | Conservative, lower cost |
| Daily | $0 | ~$17 | Minimal cost, only catches slow-moving markets |

Backtest costs: `historical`/`midpoint`/`ollama` estimators are free. `cached-claude` costs ~$2-5 one-time.

---

## Troubleshooting

**Database won't start:**
```bash
podman-compose down -v  # remove volumes (WARNING: deletes all data)
podman-compose up -d polyagent-db
```

**Tests fail with ModuleNotFoundError:**
```bash
source .venv/bin/activate
uv pip install -e ".[dev]"
```

**Bot crashes on startup:**
```bash
# Check if database is healthy
podman ps --filter name=polyagent-db
# Check if .env has all required vars
grep ANTHROPIC_API_KEY .env
```

**Ollama unreachable:**
```bash
# Verify connectivity
curl -s http://192.168.1.56:11434/api/tags
# If unreachable, the bot falls back to midpoint estimates automatically
# Check LXC is running and port 11434 is open
# To disable Ollama: set OLLAMA_ENABLED=false in .env
```

**Data ingestion fails:**
```bash
# Check what exists
ls -la data/goldsky/ data/processed/

# Re-process without re-downloading
polyagent ingest --process --data-dir ./data

# If snapshot download times out, increase httpx timeout or use --full
polyagent ingest --full --data-dir ./data
```

**No markets passing scanner:**
```bash
# Lower thresholds temporarily to debug
# In .env:
MIN_GAP=0.05
MIN_DEPTH=200
```

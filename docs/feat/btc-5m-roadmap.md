# BTC Short-Horizon Subsystem — Phased Roadmap

Companion document to `btc-5m-subsystem.md` (spec) and
`btc-5m-phase1-plan.md` (implementation plan). Captures the sequencing,
gate conditions, and explicit non-goals for everything beyond Phase 1.

Every phase past Phase 1 is **gated on evidence**, not on schedule. If the
gate for Phase N fails, we do not build Phase N. Stopping is always a
valid outcome.

## Phase 1 — Paper MVP — **DONE** (merged 2026-04-24)

**What shipped:** single worker thread that scans Polymarket for
`btc-updown-(5m|15m)-<ts>` markets, estimates P(up) via lognormal Φ(d₂)
against Coinbase BTC/USD spot + rolling realized vol, records paper
trades when |edge| clears a configurable threshold, resolves markets
whose window has ended, and exposes `polyagent btc5m-stats`.

**What it delivers:** two to four weeks of online paper-trade data with
enough instrumentation to decide whether the math has edge.

**Remaining operational work** (handled outside code):
- Apply migration `005_btc5m.sql` on the deployment host.
- Set `BTC5M_ENABLED=true` and restart the bot.
- Optional: submit Polymarket's sponsored Chainlink Data Streams key
  application — parallel track, doesn't block anything.

## Phase 2 — Live trading

**Gate (all three must hold at the 14-day checkpoint):**

| Metric | Threshold |
|---|---|
| Total paper P&L | ≥ $5 |
| Win rate on above-threshold trades | ≥ 52% |
| Coinbase→Chainlink basis mis-resolutions | ≤ 1 per 100 |

Breakeven on a 1¢ Polymarket spread is ~50.5%; the 52% threshold has a
real-edge buffer. "Total P&L ≥ $5" deliberately is not "just positive"
— tiny positive is probably noise.

**Build:** flip paper to live. Ship a separate `QUANT_SHORT_LIVE_ENABLED`
env var (independent of the main bot's `POLYMARKET_LIVE_ENABLED`).
Replace the paper `insert_trade` call in `QuantDecider` with order
submission through the existing `PolymarketClient.place_order` path. Add
three guards:

- Hard bankroll cap on the sub-bot (separate from main bot bankroll,
  $100 to $200 suggested to start).
- Per-day loss kill switch (e.g. -$5 loss disables for 24h).
- Fixed $5 notional per trade until >= 50 realized trades exist, then
  switch to Kelly sizing fitted to realized edge statistics.

**Also unify the trade ledger.** The short-horizon paper-trading work
was deliberately kept on a separate `quant_short_trades` table so the
14-day gate evaluation could be read cleanly without main-bot trades
mixed in. That separation has served its purpose at gate-time and now
becomes operational drift. As part of the live flip:

- Write live short-horizon trades to the main `positions` table tagged
  `market_class='quant_short'` (the column already exists from the
  classifier feature).
- Add a `quant_short_audit` sidecar table with `FK position_id` carrying
  the math-specific columns (`estimator_p_up`, `vol_at_decision`,
  `edge_at_decision`, `price_source_id`). Keeps `positions` narrow.
- Refactor `polyagent quant-stats` to be a thin view over
  `positions JOIN quant_short_audit` filtered by `market_class`.
- Drop `quant_short_trades` in a follow-up migration once the audit
  retention window passes.
- Delete the "Quant Short-Horizon (paper)" section from
  `polyagent/cli/performance.py` (the interim split-ledger surface,
  shipped during Phase 1 paper-trading) since live trades will land in
  the main `positions` rollup automatically.
- The strike-market handler already writes through this pipeline today.
  Phase 2 makes short-horizon consistent with strike.

**Effort:** ~1 day for the live flip itself plus ~half day for the
ledger unification. All decision logic already exists; we're replacing
a `quant_short_trades` insert with a position write through
`ExecutorService.place_order`.

## Phase 3 — Chainlink Data Feed swap (DONE, Data Streams pending)

Trigger arrived early: `polyagent quant-validate` showed 43% of paper-trade
outcomes disagreed with Polymarket settlements, with a $15 bias over 46
trades. Root cause was Coinbase-to-Chainlink basis on near-flat 5m and
15m windows.

**Built:** `ChainlinkDataFeedSource` reads the on-chain Polygon
AggregatorV3 contract for BTC/USD via JSON-RPC `eth_call`. Implements
both PriceSource (`tick`, `current`, `realized_vol`) and
SettlementSource (`price_at`, `source_id`) protocols, so registry swap
was a one-line factory change. RPC endpoint is overridable via
`POLYGON_RPC_URL` env (defaults to `https://polygon-rpc.com`).

`paper_only=True` stays on until ~24h of paper trades on the new source
confirm the gate calibration is sane; flip via `QUANT_BTC_PAPER_ONLY=false`
when ready.

**Still open:** Chainlink Data Streams (the off-chain low-latency feed,
~1s update vs ~60s on-chain heartbeat) requires a sponsored API key
from Polymarket. Worth pursuing if Phase 4 latency telemetry shows the
60s heartbeat is bleeding edge to other 5m traders.

**Effort actual:** ~half day (Data Feed via Polygon RPC, no web3.py dep,
manual ABI encoding).

## Phase 4 — Latency upgrades (likely unnecessary)

**Gate:** live numbers show systematic adverse selection — our fills
are landing on the ask right before price moves down, suggesting
book-staleness on our side relative to the market.

**Build:** Coinbase WebSocket feed for sub-second spot (replacing the
2-second REST poll). Possibly Polymarket WebSocket for book updates
if they expose one.

**Effort:** ~2–3 days. The main cost is that PolyAgent is sync-only
today; adding async + websockets is architecturally novel.

**Expected outcome: we don't do this.** 5-minute windows move slowly
relative to book ticks; the REST cadence is almost certainly fine for
this timeframe. Phase 4 only makes sense if we later add 1-minute
markets (which Polymarket doesn't currently list for BTC).

## Phase 5 — Cross-tenor arbitrage

**Gate:** both 5m and 15m showing durable edge from Phase 2, and
observed cases where their implied BTC distributions are inconsistent
(e.g., three consecutive 5m markets imply a different 15-minute P(up)
than a single 15m market covering the same span).

**Build:** new "tenor arb" module that monitors coverage-equivalent
combinations of 5m and 15m markets, prices them against a single
estimator, and trades the divergence. Non-trivial — we now hold
multiple correlated positions simultaneously and need to track their
joint risk.

**Effort:** ~1 week.

**Expected outcome:** low probability of real profit. Sophisticated
MMs likely keep 5m/15m pricing internally consistent already. But the
signal of divergence itself is a useful monitoring tool: sustained
divergence between tenors usually means one of them is priced wrong
on the retail side.

## Explicit non-goals

These are declined, not "we'll get to them later":

- **Asset expansion** to ETH, SOL, XRP, DOGE. Directional rule from the
  user: BTC only; expansion happens across timeframes, not across
  assets.
- **ML trend classifier.** OddArbTrading's RandomForest was trained on
  1-hour sports markets and never transferred. We don't have the
  data scale to make ML help here, and a math model is mechanistic
  enough to debug.
- **LLM brain involvement.** The whole point of this subsystem is to
  bypass the LLM for quantitative markets where math is better than
  reasoning.
- **1-minute BTC binary markets.** Polymarket doesn't list them. If
  they ever do, that triggers a separate design conversation — the
  latency profile is fundamentally different.

## Cross-cutting: main-bot classifier loop

Unrelated to this subsystem, the market classifier shipped in the
earlier session (`docs/feat/market-classifier-analytics.md`) is now
accumulating per-class data on the main bot. After ~2 weeks of its
own data, a separate decision point arrives: which slow-market class
is actually worth trading (or blocking). That decision is
orthogonal to the BTC sub-bot — they trade different instruments
with different edge sources.

## Sequencing summary

```
Phase 1 ✓ ────► deploy, 14-day paper run
                   │
                   ├── gate passes ─► Phase 2 (live, ~1 day)
                   │                      │
                   │                      ├── basis matters ─► Phase 3 (~1 day)
                   │                      │
                   │                      ├── adverse selection ─► Phase 4 (~2-3 days)
                   │                      │
                   │                      └── cross-tenor signal ─► Phase 5 (~1 week)
                   │
                   └── gate fails ─► STOP. Keep the infra running,
                                      revisit if Chainlink credentials
                                      arrive and change the basis story.
```

The infrastructure cost of keeping Phase 1 running after a gate
failure is negligible (a few Coinbase API polls per minute), so
there's no pressure to delete it. Leave it on, let data accumulate,
re-evaluate monthly.

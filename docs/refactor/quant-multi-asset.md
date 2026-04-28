# Multi-Asset Quant Subsystem Refactor

## Context

Two related quant subsystems exist today, both built around the same lognormal Φ(d₂) estimator but with hardcoded asset wiring.

`polyagent/services/btc5m/` runs the short-horizon binary up/down paper-trading worker. It owns its own scanner, decider, resolver, worker loop, and tables (`btc5m_markets`, `btc5m_trades`). Its slug regex (`^btc-updown-(\d+[mhd])-(\d+)$`) and spot wiring (`BtcSpotSource = CoinbaseSpotSource("BTC-USD")`) make it BTC-only by name and by code, even though the math is asset-agnostic.

`polyagent/services/crypto_quant/` is the integration seam between the LLM brain and Φ(d₂) for long-horizon strike markets ("Will the price of Bitcoin be above $X on …"). It supports BTC and ETH today via a hardcoded question-parser regex tuple. It has no own tables; it produces a Thesis-shaped result that the brain consumes through the existing `positions` pipeline.

Both subsystems will need to scale to multi-timeframe and multi-asset coverage as Polymarket adds more crypto, commodity (gold, oil), and FX (EUR/USD, GBP/USD) markets. Each new asset today would require parallel edits in both packages plus configuration sprawl in `infra/config.py` and `.env`.

This refactor unifies the two trees into a single `services/quant/` package backed by an asset registry, with a hand-rolled migration runner so prod schema can no longer drift behind the repo.

## Decisions

The following decisions were made interactively and lock the design surface for this refactor.

| # | Decision | Rationale |
|---|---|---|
| Q1 | Refactor only; BTC + ETH stay live, no new live assets in this work | Validating the abstraction with the assets we already understand keeps blast radius small. New assets become follow-up tickets. |
| Q2 | Full unification: one `services/quant/` package replaces both `btc5m/` and `crypto_quant/` | Drift between two near-duplicate trees is the failure mode we are eliminating. Halfway unification preserves it. |
| Q3 | Python registry (typed `AssetSpec` dataclasses) is the source of truth for asset declarations | Adding an asset requires code anyway (a question parser, a spot source class); type safety + grep-ability beats YAML's "config-only" advantage that does not apply here. |
| Q4 | Mismatched price/settlement source → `paper_only=True` on the spec; the orchestrator refuses to live-trade those assets regardless of `LIVE_ENABLED` | Encodes the rule mechanically rather than relying on operators reading metrics. Paper trading still accumulates calibration data. |
| Q5 | Single orchestrator thread with per-asset poll cadences | At 3-10 assets polling at 2-60s, one thread is enough. Per-asset threads buy fault isolation we do not need at this scale and double the surface area. |
| Q6 | Rename `btc5m_*` tables to `quant_short_*` via DDL migration; tables are empty, ALTER TABLE is instant | The whole point of the refactor is to stop encoding asset assumptions in names. Backwards compat is not required (no external consumers). |
| Q7 | Hand-rolled `polyagent migrate {up,status,baseline}` CLI, ~80 lines, zero new dependencies | Alembic's autogen value depends on SQLAlchemy models we do not have. Yoyo adds a dependency for behavior we can write in less code than its config takes. |
| Q8 | Registry owns per-asset defaults; env vars override per-asset for fast iteration | Defaults in version-controlled code, knobs in env for paper-trading calibration loops. Promote stable overrides into the registry over time. |

## Package layout

```
polyagent/services/quant/
├── __init__.py
├── assets/
│   ├── __init__.py
│   ├── registry.py          # ASSETS: dict[str, AssetSpec]; small read API
│   ├── spec.py              # AssetSpec, AssetClass, MarketFamily, VolMethod
│   └── sources/
│       ├── __init__.py
│       ├── coinbase.py      # CoinbaseSpotSource (PriceSource + SettlementSource)
│       ├── chainlink.py     # placeholder until Data Streams creds arrive
│       └── (future) fx.py, lbma.py, polygon.py
├── core/
│   ├── __init__.py
│   ├── estimator.py         # Φ(d₂); moved from btc5m/estimator.py
│   ├── vol.py               # compute_vol(spec, source, horizon_s) + VolCalibration
│   └── pnl.py               # binary-option P&L; moved from btc5m/resolver.py
├── short_horizon/
│   ├── __init__.py
│   ├── scanner.py           # generalized slug regex per registered asset
│   ├── decider.py           # asset-aware paper-trade executor
│   ├── resolver.py          # writes audit row recording feed used + Polymarket source
│   └── repository.py        # quant_short_markets / quant_short_trades CRUD
├── strike/
│   ├── __init__.py
│   ├── parser.py            # generalized question parser, iterates registry
│   └── service.py           # QuantStrikeService — brain integration seam
├── orchestrator.py          # single worker thread; was btc5m/worker.py
└── cli/
    ├── __init__.py
    └── stats.py             # generalizes btc5m-stats → quant-stats --asset BTC
```

The old `btc5m/` and `crypto_quant/` directories are deleted at the end of the build sequence.

## Asset registry

```python
# polyagent/services/quant/assets/spec.py
from dataclasses import dataclass
from enum import Enum
from typing import Callable

class AssetClass(str, Enum):
    CRYPTO = "CRYPTO"
    FX = "FX"
    COMMODITY = "COMMODITY"

class MarketFamily(str, Enum):
    SHORT_HORIZON = "SHORT_HORIZON"   # btc-updown-5m-...
    STRIKE = "STRIKE"                 # "Will BTC be above $X on ..."
    RANGE = "RANGE"                   # "Will BTC be between $X and $Y ..."

class VolMethod(str, Enum):
    ROLLING_REALIZED = "ROLLING_REALIZED"
    FIXED = "FIXED"
    HYBRID = "HYBRID"

PriceSourceFactory = Callable[[], "PriceSource"]
SettlementSourceFactory = Callable[[], "SettlementSource"]

@dataclass(frozen=True)
class VolCalibration:
    method: VolMethod
    rolling_min_s: int = 300
    rolling_max_s: int = 24 * 3600
    rolling_horizon_multiplier: float = 4.0
    fixed_value: float | None = None
    hybrid_threshold_s: int = 4 * 3600

@dataclass(frozen=True)
class AssetSpec:
    asset_id: str
    asset_class: AssetClass
    price_source: PriceSourceFactory
    settlement_source: SettlementSourceFactory
    default_vol: float
    vol_calibration: VolCalibration
    supported_market_families: frozenset[MarketFamily]
    paper_only: bool = False
    fee_bps: float = 0.0
    edge_threshold: float = 0.05
    tick_interval_s: float = 2.0
    slug_token: str = ""              # e.g. "btc" for short_horizon slug matching
    question_keywords: tuple[str, ...] = ()  # e.g. ("Bitcoin", "BTC") for strike parser
```

```python
# polyagent/services/quant/assets/registry.py
from polyagent.services.quant.assets.spec import (
    AssetSpec, AssetClass, MarketFamily, VolCalibration, VolMethod,
)
from polyagent.services.quant.assets.sources.coinbase import CoinbaseSpotSource

ASSETS: dict[str, AssetSpec] = {
    "BTC": AssetSpec(
        asset_id="BTC",
        asset_class=AssetClass.CRYPTO,
        price_source=lambda: CoinbaseSpotSource("BTC-USD"),
        settlement_source=lambda: CoinbaseSpotSource("BTC-USD"),
        default_vol=0.60,
        vol_calibration=VolCalibration(
            method=VolMethod.HYBRID,
            rolling_min_s=300,
            rolling_max_s=24 * 3600,
            rolling_horizon_multiplier=4.0,
            fixed_value=0.60,
            hybrid_threshold_s=4 * 3600,
        ),
        supported_market_families=frozenset({
            MarketFamily.SHORT_HORIZON, MarketFamily.STRIKE, MarketFamily.RANGE,
        }),
        paper_only=False,
        fee_bps=0.0,
        edge_threshold=0.05,
        tick_interval_s=2.0,
        slug_token="btc",
        question_keywords=("Bitcoin", "BTC"),
    ),
    "ETH": AssetSpec(
        asset_id="ETH",
        asset_class=AssetClass.CRYPTO,
        price_source=lambda: CoinbaseSpotSource("ETH-USD"),
        settlement_source=lambda: CoinbaseSpotSource("ETH-USD"),
        default_vol=0.75,
        vol_calibration=VolCalibration(
            method=VolMethod.HYBRID,
            fixed_value=0.75,
            hybrid_threshold_s=4 * 3600,
        ),
        supported_market_families=frozenset({MarketFamily.STRIKE, MarketFamily.RANGE}),
        # ETH short_horizon support disabled until we confirm Polymarket lists ETH up/down.
        paper_only=False,
        fee_bps=0.0,
        edge_threshold=0.05,
        slug_token="eth",
        question_keywords=("Ethereum", "ETH"),
    ),
}

def get(asset_id: str) -> AssetSpec | None:
    return ASSETS.get(asset_id)

def enabled_for(family: MarketFamily) -> list[AssetSpec]:
    return [s for s in ASSETS.values() if family in s.supported_market_families]

def live_eligible(family: MarketFamily) -> list[AssetSpec]:
    return [s for s in enabled_for(family) if not s.paper_only]
```

Env-var overrides resolve via a small `apply_env_overrides(spec) -> AssetSpec` helper invoked at app startup, returning a new frozen `AssetSpec` with updated fields. Supported overrides:

| Env var | Field |
|---|---|
| `QUANT_<ASSET>_VOL` | `default_vol`, and `VolCalibration.fixed_value` if HYBRID/FIXED |
| `QUANT_<ASSET>_EDGE_THRESHOLD` | `edge_threshold` |
| `QUANT_<ASSET>_FEE_BPS` | `fee_bps` |
| `QUANT_<ASSET>_PAPER_ONLY` | `paper_only` |

## Source abstractions

Two protocols, deliberately separate. `BtcSpotSource` today does both jobs, with a `CoinbaseCandleHistory` hacked in for resolution. They have different shapes and different cost profiles (one is hot-path tick-frequency, the other is occasional history lookup).

```python
# polyagent/services/quant/assets/sources/base.py (Protocols)
from datetime import datetime
from decimal import Decimal
from typing import Protocol

class PriceSource(Protocol):
    """Live ticks fed to the estimator. Cheap, frequent."""
    def tick(self) -> Decimal | None: ...
    def current(self) -> Decimal | None: ...
    def realized_vol(self, window_s: int) -> float: ...
    def close(self) -> None: ...

class SettlementSource(Protocol):
    """Historical lookup at a specific past timestamp for resolving paper trades."""
    def price_at(self, ts: datetime) -> Decimal | None: ...
    def source_id(self) -> str: ...      # e.g. "coinbase:BTC-USD" — recorded for audit
```

`CoinbaseSpotSource` implements both protocols. Future `ChainlinkSpotSource`, `EcbReferenceFxSource`, `LbmaGoldFixSource`, etc. implement whichever apply.

`paper_only` enforcement lives in two places.

1. The orchestrator filters `registry.live_eligible(family)` when building its decider; trades for `paper_only=True` assets always go through the paper path regardless of `QUANT_LIVE_ENABLED`.
2. A startup self-check logs each `paper_only` asset with the reason: `WARN quant: asset=ETH paper_only=true (Coinbase price source, Polymarket settles via Chainlink)`. Operators see this without grepping the registry.

## Vol calibration

Single function `compute_vol(spec: AssetSpec, source: PriceSource, horizon_s: float) -> float` lives in `quant/core/vol.py`. Decider and strike service both call it. One place to change vol math.

Behavior by `VolMethod`:

- `ROLLING_REALIZED`: `lookback = clamp(rolling_min_s, k * horizon_s, rolling_max_s)`; returns `source.realized_vol(lookback)`. Falls back to `default_vol` if rolling returns 0 (insufficient samples).
- `FIXED`: returns `default_vol` (or `fixed_value` if set).
- `HYBRID`: ROLLING_REALIZED for `horizon_s < hybrid_threshold_s`, else FIXED. Matches today's behavior in `crypto_quant/` (long-horizon strike markets get `default_vol`) and today's behavior in `btc5m/` (short-horizon gets rolling realized) without regression.

The `rolling_horizon_multiplier=4.0` default means a 5m market looks back 20m of ticks (clamped to `rolling_min_s=300`, so effectively 5m), a 1h market looks back 4h of ticks, a 4h market looks back 16h of ticks. Beyond that the HYBRID threshold kicks in and we fall back to `default_vol`.

## Orchestrator

```
on startup:
    sources = {a.asset_id: a.price_source() for a in registry.enabled_for(SHORT_HORIZON)}
    settlements = {a.asset_id: a.settlement_source() for a in registry.enabled_for(SHORT_HORIZON)}
    scanner = QuantSlugScanner(registry)
    decider = QuantDecider(registry, sources, repo, settings)
    resolver = QuantResolver(registry, settlements, repo)
    last_tick_at = {asset_id: 0.0 for asset_id in sources}
    last_market_poll = 0.0

loop until shutdown:
    now = time.time()
    for asset_id, src in sources.items():
        spec = registry.get(asset_id)
        if now - last_tick_at[asset_id] >= spec.tick_interval_s:
            safely(src.tick)
            last_tick_at[asset_id] = now

    if now - last_market_poll >= market_interval_s:
        safely(scan_and_upsert, scanner, repo)
        for row in repo.get_active_markets(now):
            safely(decider.evaluate, row)
        safely(resolver.resolve_due_markets)
        last_market_poll = now

    sleep(min(spec.tick_interval_s for spec in registry.enabled_for(SHORT_HORIZON)))
```

`safely(fn, *args)` is a thin try/except → `logger.exception` → continue. Asset failures are isolated; one flaky source never aborts the others.

`QuantSlugScanner` consults the registry's enabled-for-SHORT_HORIZON list and matches `^(btc|eth|...)-updown-(\d+[mhd])-(\d+)$` against Gamma. Each match becomes a `QuantShortMarket` row tagged with `asset_id`.

`QuantDecider.evaluate(market_row)`:

1. Look up `spec = registry.get(market_row["asset_id"])`. If missing, log and skip (race with a removed asset).
2. Pull `source = sources[market_row["asset_id"]]`.
3. Compute `vol = compute_vol(spec, source, horizon_s=ttm_seconds)`.
4. Run Φ(d₂); compute `edge = p_up - mid`.
5. Reject if `abs(edge) < spec.edge_threshold`.
6. Reject if `gross_edge_usd <= fees_usd` (using `spec.fee_bps`).
7. Insert paper trade or submit live order based on `spec.paper_only` + `QUANT_LIVE_ENABLED`.

`QuantResolver.resolve_due_markets()` for each unresolved market past `window_end_ts`:

1. Pull `settlement_source` for the asset; fetch `price_at(window_start_ts)` and `price_at(window_end_ts)`.
2. Determine outcome.
3. Write `quant_short_markets` resolution row with `start_spot`, `end_spot`, `outcome`, plus a `price_source_id` audit field (`spec.settlement_source().source_id()`) so we can later compare against Polymarket's reported settlement price to measure basis empirically.
4. Compute and persist P&L for unresolved trades.

## Brain seam

```python
# polyagent/services/quant/strike/service.py
class QuantStrikeService:
    """Brain integration seam — replaces CryptoQuantService with identical surface."""

    def __init__(self, sources: dict[str, PriceSource]) -> None:
        self._sources = sources

    def matches(self, question: str) -> ParsedStrike | None:
        return parse_question(question)   # iterates registry.enabled_for(STRIKE)

    def evaluate(self, question: str, hours_to_resolution: float) -> tuple[...] | None:
        strike = self.matches(question)
        if strike is None:
            return None
        spec = registry.get(strike.asset_id)
        if spec is None or MarketFamily.STRIKE not in spec.supported_market_families:
            return None
        source = self._sources.get(strike.asset_id)
        if source is None:
            return None
        spot = source.current()
        if spot is None or spot <= 0:
            return None
        vol = compute_vol(spec, source, horizon_s=hours_to_resolution * 3600)
        result = estimate_yes_probability(strike, spot, vol, hours_to_resolution)
        thesis_text = build_thesis_text(strike, spot, vol, result)
        return strike, result, thesis_text
```

Same `(strike, QuantResult, thesis_text)` shape `CryptoQuantService.evaluate` returns today, so `polyagent/services/brain.py` only needs an import rename.

The strike service shares the orchestrator's `PriceSource` dict — one Coinbase BTC ticker per process, polled by the orchestrator, read by the strike service. Orchestrator startup happens before brain construction; the registry exposes the live source dict via a small `bind_sources(dict[str, PriceSource])` setter or via direct injection at construction time.

## Database changes

### Migration 006 — rename + asset_id column

```sql
-- db/migrations/006_quant_short_rename.sql
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
```

The `DEFAULT 'BTC' → DROP DEFAULT` pattern backfills any existing rows in one statement without a separate UPDATE pass. `price_source_id` records which feed resolved the market, used for empirical basis measurement.

### Migration 007 — schema_migrations bootstrap

The migration runner creates this table on first invocation if absent (chicken-and-egg). For documentation:

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

## Migration runner

`polyagent/scripts/migrate.py` exposes a CLI subcommand:

- `polyagent migrate up` — apply pending migrations
- `polyagent migrate status` — list applied + pending + drifted versions
- `polyagent migrate baseline` — record all existing migration files as already applied without executing them; used once on prod immediately after deploy to seed `schema_migrations` with versions 001-005

Algorithm for `up`:

1. Connect to DB. Ensure `schema_migrations` exists (idempotent CREATE).
2. List `db/migrations/*.sql` sorted by filename. Extract `version` as the prefix before the first underscore (`006_quant_short_rename.sql` → `"006"`).
3. For each file, lookup by version in `schema_migrations`:
   - **Not present**: BEGIN; execute SQL; INSERT into `schema_migrations` with version, filename, sha256 checksum; COMMIT. On error: ROLLBACK + exit non-zero.
   - **Present, checksums match**: skip.
   - **Present, checksums differ**: print error, exit non-zero. Operator must investigate (someone hand-edited an applied migration, or reverted the file).

CI runs `migrate up` against an ephemeral Postgres on every PR. Deploy hooks run it before the bot starts (`ExecStartPre=` in systemd or equivalent in compose).

## Build sequence

Six PRs, each independently mergeable. Prod stays green throughout.

| PR | Scope | Behavior change |
|---|---|---|
| 1 | Migration runner + schema_migrations | None. After merge: deploy, run `migrate baseline`, wire `migrate up` into startup. |
| 2 | Move estimator + extract `compute_vol` to `quant/core/` | None. Old import paths re-exported for one PR cycle. |
| 3 | `AssetSpec` + registry + Coinbase source under `quant/assets/` | None. Pure addition; nothing consumes the registry yet. |
| 4 | Migrate `crypto_quant` → `quant/strike` consuming the registry; one-line brain.py import change; delete `crypto_quant/` | Behavior-preserving for BTC + ETH strike markets. Regression-tested via existing brain tests. |
| 5 | Migration 006 + repository rename | Tables renamed; `Btc5mRepository` → `QuantShortRepository`; models renamed. Worker still uses old (re-exported) interfaces until PR 6. Run `migrate up` on deploy. |
| 6 | Orchestrator replaces `run_btc5m_worker`; decider/scanner/resolver consume registry; delete `btc5m/`; rename env vars `BTC5M_*` → `QUANT_SHORT_*`, add `QUANT_<ASSET>_*` per Q8 | Multi-asset capable. One-time `.env` update on deploy. |

After PR 6: BTC + ETH live (same as today). Adding a new asset is one `AssetSpec` entry plus possibly a new `PriceSource` class.

## Testing strategy

TDD throughout per project standards.

| Layer | Test type | Coverage |
|---|---|---|
| `quant/core/{estimator,vol,pnl}` | Unit | Pure-math edge cases. Vol calibration: clamp behavior, hybrid threshold, fixed fallback, zero-sample fallback to default. |
| `quant/assets/sources/coinbase` | Unit | `tick`/`current`/`realized_vol`/`price_at` against fake httpx; `source_id()` stable. |
| `quant/assets/registry` | Unit | `enabled_for`, `live_eligible`, env override application produces correct frozen specs. |
| `quant/short_horizon/{scanner,decider,resolver}` | Unit | Generalized slug regex against fixtures for BTC, ETH, future; decider dispatches to correct source; resolver writes audit row. |
| `quant/strike/{parser,service}` | Unit | Question parser fixtures per registered asset; `matches`/`evaluate` returns identical-shape result to today's `CryptoQuantService` for BTC + ETH (golden-file regression). |
| `quant/orchestrator` | Integration | Fake registry with two assets, in-memory repo: per-asset poll cadence, fault isolation (one source raises → others continue), shutdown clean. |
| `polyagent/scripts/migrate` | Integration | Empty Postgres → `migrate up` applies all → second run is no-op → edit a migration file → next run exits non-zero with drift error. CI runs against ephemeral DB. |
| Brain regression | Integration | Existing brain tests pass unchanged after PR 4 import swap. |

## Non-goals

These are explicitly out of scope for this refactor. They become follow-up tickets if/when needed.

- **No new asset classes.** No FX, no commodities, no SOL, no XRP. Q1.
- **No `quant_strike_*` paper-trade tables.** Strike markets continue to use the main bot's `positions` pipeline.
- **No Chainlink source implementation.** Class skeleton may be added; behavior comes when Data Streams creds arrive.
- **No `LIVE_ENABLED` flip for new assets.** Anything beyond what's already live stays paper-only until a separate calibration decision.
- **No backtest engine integration with the new structure.** Backtests keep using their existing path.
- **No automatic `paper_only` detection.** The flag is set manually in the registry per asset; the system enforces it but does not infer it from runtime feed metadata.

## Open questions

None blocking. The following are deferred to implementation:

- Exact CI shape (GitHub Actions vs other) for `migrate up` on ephemeral Postgres.
- Whether `quant_resolution_audit` becomes its own table or lives inline as `price_source_id` on `quant_short_markets`. Current design picks the inline column; revisit if we need to record multiple feeds per resolution.
- Where the strike-service `bind_sources` plumbing lives (constructor injection vs setter). Decided during implementation — no architectural impact.

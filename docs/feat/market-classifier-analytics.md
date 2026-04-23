# Market Classifier & Per-Class Analytics

## Motivation

Paper trading has accumulated ~19 closed positions so far. The distribution
is heavily skewed toward sports (tennis, MLB, NBA, NHL, soccer, esports) with
one crypto trade. Net P&L is slightly negative, but the more important
observation is that we cannot currently answer "which *classes* of markets
are we profitable on?" without manually grepping question text.

The instinct to "block sports" is premature: all four winning trades in the
sample were sports SELLs, which might be genuine edge or small-sample noise.
Before blocking anything, we need the instrumentation to tell signal from
noise.

This spec adds a lightweight market classifier and per-class analytics. No
behavioral change to the pipeline. After a few more weeks of paper trading,
data from this feature will drive the follow-up decision — whether to block a
class, tighten per-class scanner thresholds, or tune the brain prompt per
class.

## Goals

- Tag every market at scan time with a coarse class:
  `sports | crypto | politics | macro | other`.
- Persist the class on the `markets` row so positions can derive it via join.
- Expose a CLI (`polyagent class-stats`) that reports trades, win rate,
  total P&L, avg P&L, and avg hold time per class.
- Retroactively classify existing `markets` rows so the first `class-stats`
  run includes historical paper trades.

## Non-Goals

- No blocking, throttling, or per-class brain tuning in this change.
- No subclass granularity (tennis vs. MLB, btc_snapshot vs. eth_target, etc.).
- No dashboard UI beyond a single Rich table.
- No change to scanner kill filters, brain thresholds, or executor sizing.

## Architecture

```
scanner_worker (main.py)
  -> scanner.scan_batch(...)                            (unchanged)
  -> for each (market, score) in survivors:
       market.market_class = classify(market.question, market.category)
       market_repo.upsert(market, MarketStatus.QUEUED)  (reads market.market_class)
       ...

markets table: market_class column (ENUM, indexed)

polyagent class-stats                                   (new CLI command)
  JOIN positions -> markets, GROUP BY market_class
```

One new service module (`classifier.py`), one modified call site in
`main.py`, one modified repository (SQL adds the column, signature
unchanged — reads `market.market_class`), one schema migration, one new CLI
subcommand, one backfill script.

## Data Model

### New enum

```python
# polyagent/models.py
class MarketClass(StrEnum):
    SPORTS = "sports"
    CRYPTO = "crypto"
    POLITICS = "politics"
    MACRO = "macro"
    OTHER = "other"
```

`MarketData` gains an optional attribute:

```python
@dataclass
class MarketData:
    ...
    market_class: MarketClass | None = None
```

Optional because it's populated by the scanner after construction, not by
`PolymarketClient.parse_market`. This keeps the parser a pure data-shape
translation with no classification logic.

### Schema migration `004_market_class.sql`

```sql
-- db/migrations/004_market_class.sql
-- Add market_class for per-class analytics and future per-class policies.

CREATE TYPE market_class AS ENUM ('sports', 'crypto', 'politics', 'macro', 'other');

ALTER TABLE markets
    ADD COLUMN IF NOT EXISTS market_class market_class NOT NULL DEFAULT 'other';

CREATE INDEX IF NOT EXISTS idx_markets_class ON markets(market_class);
```

Positions does **not** get a `market_class` column. Class is stable for a
given question, and every closed position has a live FK to the market row.
A join is cheaper than a duplicated column we'd have to keep in sync.

### Retroactive backfill

`polyagent/scripts/backfill_market_class.py` — one-shot idempotent script.

1. `SELECT id, question, category FROM markets` (all rows — the table is small).
2. For each row, call `classify(question, category)` in-process. `classify`
   never raises, so every row yields a class.
3. `UPDATE markets SET market_class = %s WHERE id = %s`, committed per row.
   Per-row commits mean a transient DB error on one row doesn't roll back
   work already done; the script can be re-run to cover any rows left with
   the default `other`.
4. Log counts per class at the end.

Safe to re-run after classifier rule tweaks — it just rewrites the column.

## Classifier Rules

Single ordered cascade in `polyagent/services/classifier.py`. First match
wins. All patterns are compiled regex with `re.IGNORECASE`. Word boundaries
(`\b`) are shown explicitly where they matter; patterns without `\b` are
plain substrings embedded in a regex (safe for names and phrases that aren't
prefixes of unrelated words).

1. **crypto** — question contains any of:
   `bitcoin`, `btc`, `ethereum`, `eth`, `solana`, `\bsol\b`, `xrp`,
   `dogecoin`, `doge`, `crypto`, `stablecoin`, `usdc`, `usdt`.
2. **sports** — question or Polymarket `category` indicates a sporting event:
   - `category` equals `Sports` (case-insensitive), OR
   - question matches any of: ` vs\. `, ` vs `, ` Open:`, `\bBO3\b`,
     `\bBO5\b`, `Spread:`, `\bMLB\b`, `\bNBA\b`, `\bNHL\b`, `\bNFL\b`,
     `\bNCAA[MF]?\b`, `UEFA`, `\bLCK\b`, `\bLoL\b`, `\bDota\b`, `\bCS2\b`,
     `Valorant`, `Premier League`, `La Liga`, `Bundesliga`, `Serie A`,
     OR matches `^Will .+ win on \d{4}-\d{2}-\d{2}\??$` (sports-day pattern).
3. **politics** — question or `category` indicates politics:
   - `category` equals `Politics` (case-insensitive), OR
   - question contains any of: `president`, `\belection\b`, `primary`,
     `\bSenate\b`, `\bCongress\b`, `Supreme Court`, `Trump`, `Biden`,
     `Harris`, `Vance`, `governor`, `impeach`.
4. **macro** — question contains any of: `\bCPI\b`, `inflation`, `\bFed\b`,
   `FOMC`, `interest rate`, `recession`, `\bGDP\b`, `unemployment`,
   `jobs report`, `payrolls`.
5. **other** — default.

The full rule list is a module-level constant holding pre-compiled regexes,
with one entry per class in evaluation order. Sketch (implementer fills in
the concrete pattern lists from the rules above):

```python
CLASS_RULES: list[tuple[MarketClass, list[re.Pattern[str]]]] = [
    (MarketClass.CRYPTO,   [re.compile(p, re.IGNORECASE) for p in CRYPTO_PATTERNS]),
    (MarketClass.SPORTS,   [re.compile(p, re.IGNORECASE) for p in SPORTS_PATTERNS]),
    (MarketClass.POLITICS, [re.compile(p, re.IGNORECASE) for p in POLITICS_PATTERNS]),
    (MarketClass.MACRO,    [re.compile(p, re.IGNORECASE) for p in MACRO_PATTERNS]),
]
```

The order in the list *is* the priority. Crypto is evaluated first because
a crypto question could incidentally include a sport-league keyword (e.g.
a hypothetical "Will BTC sponsor the NBA Finals by July?"); we'd rather
such edge cases land under `crypto` than `sports`. Sports is evaluated
before politics for the same reason — a politician-and-sports hybrid like
"Will Biden attend the Super Bowl?" is more coherently tagged as sports.

### Public API

```python
def classify(question: str, category: str) -> MarketClass:
    """Return the coarse class of a market, never raising."""
```

Pure function. No I/O. Never raises. Returns `MarketClass.OTHER` if no rule
matches.

## Wiring

### Scanner

In `polyagent/services/scanner.py`, `scan_batch` stays focused on kill
filters. Classification happens at the main.py call site — the place that
already owns the upsert. This avoids coupling the scanner to the classifier:

```python
# polyagent/main.py (scanner_worker)
for market, score in survivors:
    market.market_class = classify(market.question, market.category)
    db_id = market_repo.upsert(market, MarketStatus.QUEUED)
    ...
```

### Repository

`MarketRepository.upsert` reads `market.market_class` (defaulting to
`MarketClass.OTHER` if unset) and persists it. The method signature does
*not* change — the new value rides on the existing `MarketData` argument.
The `UPSERT_MARKET` SQL gains one column in both the insert column list and
the conflict UPDATE clause, so a market's class is refreshed if the
classifier rules change and the same polymarket_id is rescanned.

## CLI: `class-stats`

Added as a new file `polyagent/cli/class_stats.py`, registered in
`polyagent/cli/main.py`.

```sql
SELECT
    m.market_class AS class,
    COUNT(*) FILTER (WHERE p.status = 'closed')                     AS trades,
    COUNT(*) FILTER (WHERE p.status = 'closed' AND p.pnl > 0)       AS wins,
    COUNT(*) FILTER (WHERE p.status = 'closed' AND p.pnl <= 0)      AS losses,
    COALESCE(SUM(p.pnl)  FILTER (WHERE p.status = 'closed'), 0)     AS total_pnl,
    COALESCE(AVG(p.pnl)  FILTER (WHERE p.status = 'closed'), 0)     AS avg_pnl,
    COALESCE(
        AVG(EXTRACT(EPOCH FROM (p.closed_at - p.opened_at)) / 3600)
        FILTER (WHERE p.status = 'closed'), 0
    )                                                               AS avg_hold_h
FROM positions p
JOIN markets m ON p.market_id = m.id
GROUP BY m.market_class
ORDER BY total_pnl DESC;
```

Rich table output:

```
         Per-Class Performance
 Class      Trades  W/L     Win%   Avg P&L   Total P&L   Avg Hold
 sports     18      4/14    22%    -$0.28    -$4.96      28h
 crypto     1       0/1     0%     -$0.10    -$0.10      18h
 politics   0       -       -      -         $0.00       -
 macro      0       -       -      -         $0.00       -
 other      0       -       -      -         $0.00       -
 TOTAL      19      4/15    21%    -$0.27    -$5.06      27h
```

Empty classes are still listed with dashes so the absence is visible.
Negative P&L rendered in red, positive in green (matching `perf` style).

### Note on existing `perf --by-category` flag

`polyagent/cli/performance.py` already declares an unused `--by-category`
flag. That flag was intended for Polymarket's own `category` field (often
`unknown` and unreliable), not our classifier. We leave it alone in this
change — a separate `class-stats` subcommand is clearer than overloading a
flag that means something different. If we later decide to retire the dead
flag, that's a cleanup, not part of this spec.

## Error Handling

- `classify` never raises — unrecognized inputs fall through to `OTHER`.
- The scanner tolerates a `None` return (it won't occur, but the type is
  `MarketClass | None` on `MarketData` so static checkers are calm).
- CLI handles an empty `positions` table gracefully (prints all-zero row).
- Backfill is idempotent; errors on one row do not abort the transaction —
  the row keeps its default `other` and we log a warning.

## Testing

- **Unit — classifier** (`tests/unit/test_classifier.py`):
  - One parametrized test per class with 5+ representative questions
    pulled from the real paper-trade history plus common Polymarket
    patterns (`"Will Bitcoin hit $80,000 on April 22?"`,
    `"Madrid Open: Cristian Garin vs Alexander Blockx"`,
    `"LoL: HANJIN BRION vs Dplus KIA (BO3)"`,
    `"Will Trump be re-elected in 2024?"`,
    `"Will CPI YoY be above 3% in May?"`,
    `"Will Taylor Swift attend the Super Bowl?"`).
  - Adversarial cases: `"Will the Tennessee Senate race be decided by X?"`
    must return `POLITICS` not `SPORTS` despite `Tennessee`.
  - Empty-string and whitespace-only questions return `OTHER`.

- **Unit — repository** (`tests/unit/test_markets_repo.py`): confirm
  `upsert` persists `market_class` and that re-upserting the same
  polymarket_id updates it on conflict.

- **Integration — backfill** (`tests/integration/test_backfill_market_class.py`):
  Insert a handful of legacy rows with default `other`, run the backfill
  script against the test DB, assert each row has the expected class.

- **Integration — class-stats CLI**
  (`tests/integration/test_class_stats_cli.py`): Load fixture positions
  across at least two classes, invoke the command with `click.testing.CliRunner`,
  parse the output, and confirm aggregates match hand-calculated values.

## Rollout

1. Land schema migration `004_market_class.sql` (adds column, enum, index).
2. Land classifier module + unit tests.
3. Land repository + scanner wiring + integration tests.
4. Land CLI command + CLI tests.
5. Run backfill script once against the live DB (`python -m
   polyagent.scripts.backfill_market_class`).
6. Verify `polyagent class-stats` shows non-zero rows for all classes that
   have historical trades.

No feature flag — the change is additive and cannot break existing
behavior.

## Open Questions

None blocking. The classifier rule list will need periodic tuning as new
question patterns appear; that happens as small, independent PRs over time.

## Follow-Up Work (out of scope for this spec)

- `BLOCKED_CLASSES` env var for per-class blocking, once data warrants it.
- Subclass tags (tennis/mlb/esports/soccer, btc_snapshot/eth_target,
  election/primary) if sports or crypto performance splits cleanly on
  subclass boundaries.
- Per-class brain prompt tuning — give the brain a class hint so it can
  apply class-appropriate priors.
- Per-class scanner thresholds (e.g. stricter `MIN_GAP` for sports if we
  decide to keep them but want only high-conviction entries).
- Retire or rewire the unused `perf --by-category` flag.

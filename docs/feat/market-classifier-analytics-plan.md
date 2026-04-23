# Market Classifier & Per-Class Analytics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tag every market at scan time with a coarse class (`sports | crypto | politics | macro | other`), persist it on the `markets` row, retroactively classify existing rows, and expose a `polyagent class-stats` CLI that reports per-class P&L, win rate, and hold time.

**Architecture:** A new pure function `classify(question, category) -> MarketClass` lives in `polyagent/services/classifier.py` and is invoked by the scanner worker in `main.py` before upserting. `MarketData` gains a `market_class` attribute; `MarketRepository.upsert` reads it and persists it via the existing SQL (one new column, no signature change). Positions derive their class via a join on `market_id` — no duplication. A one-shot idempotent Python script backfills the column for existing rows after the schema migration lands.

**Tech Stack:** Python 3.12, PostgreSQL (psycopg with `dict_row`), Click, Rich, pytest. Follows existing repo conventions: `StrEnum` for enum types, frozen dataclasses for domain values, parametrized SQL via psycopg.

**Source spec:** `docs/feat/market-classifier-analytics.md`

---

## File Structure

**Create:**
- `db/migrations/004_market_class.sql` — enum, column, index
- `polyagent/services/classifier.py` — pattern constants + `classify()`
- `polyagent/scripts/backfill_market_class.py` — one-shot backfill
- `polyagent/cli/class_stats.py` — `polyagent class-stats` command
- `tests/unit/test_classifier.py` — classifier rules
- `tests/integration/test_class_stats_cli.py` — CLI aggregates end-to-end
- `tests/integration/test_backfill_market_class.py` — backfill end-to-end

**Modify:**
- `polyagent/models.py` — add `MarketClass` enum + `MarketData.market_class` field
- `polyagent/data/repositories/markets.py` — `UPSERT_MARKET` and `SELECT_BY_STATUS` gain `market_class`
- `polyagent/main.py` — `scanner_worker` calls `classify()` before upsert
- `polyagent/cli/main.py` — register new command
- `tests/unit/test_models.py` — tests for `MarketClass` + extended `MarketData`
- `tests/unit/test_market_repo.py` — assert `market_class` is passed to SQL

---

## Task 1: Schema migration

**Files:**
- Create: `db/migrations/004_market_class.sql`

- [ ] **Step 1: Write the migration**

Create `db/migrations/004_market_class.sql`:

```sql
-- db/migrations/004_market_class.sql
-- Add market_class for per-class analytics and future per-class policies.

DO $$ BEGIN
    CREATE TYPE market_class AS ENUM ('sports', 'crypto', 'politics', 'macro', 'other');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE markets
    ADD COLUMN IF NOT EXISTS market_class market_class NOT NULL DEFAULT 'other';

CREATE INDEX IF NOT EXISTS idx_markets_class ON markets(market_class);
```

The `DO $$ ... $$` block is how Postgres handles "create type if not exists" (native `IF NOT EXISTS` isn't supported for `CREATE TYPE`). The rest uses `IF NOT EXISTS` for idempotency so the migration is safe to re-run.

- [ ] **Step 2: Apply the migration manually against the dev DB**

Run:

```bash
podman exec -i polyagent-db psql -U polyagent -d polyagent < db/migrations/004_market_class.sql
```

Expected: no errors. If `polyagent-db` container isn't running, `podman compose -f compose.yaml up -d polyagent-db` first.

- [ ] **Step 3: Verify schema**

Run:

```bash
podman exec polyagent-db psql -U polyagent -d polyagent -c "\d+ markets" | grep market_class
```

Expected output contains a line like:

```
 market_class        | market_class     |           | not null | 'other'::market_class ...
```

- [ ] **Step 4: Commit**

```bash
git add db/migrations/004_market_class.sql
git commit -m "feat(db): add market_class enum and column"
```

---

## Task 2: Domain model — `MarketClass` enum and `MarketData` field

**Files:**
- Modify: `polyagent/models.py`
- Modify: `tests/unit/test_models.py`

- [ ] **Step 1: Write failing tests for `MarketClass` and the new field**

Append to `tests/unit/test_models.py` (below the existing `TestThesisChecks` class):

```python
from polyagent.models import MarketClass


class TestMarketClass:
    def test_values(self):
        assert MarketClass.SPORTS.value == "sports"
        assert MarketClass.CRYPTO.value == "crypto"
        assert MarketClass.POLITICS.value == "politics"
        assert MarketClass.MACRO.value == "macro"
        assert MarketClass.OTHER.value == "other"

    def test_str_enum_behavior(self):
        assert MarketClass.SPORTS == "sports"

    def test_market_data_default_class_is_none(self):
        m = MarketData(
            polymarket_id="0x1", question="q", category="c",
            token_id="t", midpoint_price=Decimal("0.5"),
            bids_depth=Decimal("1"), asks_depth=Decimal("1"),
            hours_to_resolution=1.0, volume_24h=Decimal("1"),
        )
        assert m.market_class is None

    def test_market_data_accepts_class(self):
        m = MarketData(
            polymarket_id="0x1", question="q", category="c",
            token_id="t", midpoint_price=Decimal("0.5"),
            bids_depth=Decimal("1"), asks_depth=Decimal("1"),
            hours_to_resolution=1.0, volume_24h=Decimal("1"),
            market_class=MarketClass.CRYPTO,
        )
        assert m.market_class == MarketClass.CRYPTO
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/unit/test_models.py::TestMarketClass -v
```

Expected: ImportError / AttributeError on `MarketClass`, all four tests fail.

- [ ] **Step 3: Add `MarketClass` enum and field**

In `polyagent/models.py`, add near the top with other enums (after `Consensus`):

```python
class MarketClass(StrEnum):
    SPORTS = "sports"
    CRYPTO = "crypto"
    POLITICS = "politics"
    MACRO = "macro"
    OTHER = "other"
```

And extend `MarketData` — add `market_class` as the last field with a default of `None`:

```python
@dataclass
class MarketData:
    """Live market snapshot fetched from Polymarket.

    Args:
        polymarket_id: Unique market identifier (hex address).
        question: Human-readable market question.
        category: Market category (e.g. crypto, politics).
        token_id: Outcome token identifier for the YES side.
        midpoint_price: Current mid price between best bid and ask.
        bids_depth: Total USD available on the bid side.
        asks_depth: Total USD available on the ask side.
        hours_to_resolution: Hours until the market resolves.
        volume_24h: 24-hour trading volume in USD.
        market_class: Coarse classification set by the scanner before upsert.
    """
    polymarket_id: str
    question: str
    category: str
    token_id: str
    midpoint_price: Decimal
    bids_depth: Decimal
    asks_depth: Decimal
    hours_to_resolution: float
    volume_24h: Decimal
    market_class: MarketClass | None = None

    @property
    def min_depth(self) -> Decimal:
        """Returns the shallower side of the order book (liquidity constraint)."""
        return min(self.bids_depth, self.asks_depth)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/unit/test_models.py -v
```

Expected: all tests pass, including the new `TestMarketClass` ones and all existing tests.

- [ ] **Step 5: Commit**

```bash
git add polyagent/models.py tests/unit/test_models.py
git commit -m "feat(models): add MarketClass enum and MarketData.market_class"
```

---

## Task 3: Classifier module

**Files:**
- Create: `polyagent/services/classifier.py`
- Create: `tests/unit/test_classifier.py`

- [ ] **Step 1: Write failing tests for all classes**

Create `tests/unit/test_classifier.py`:

```python
"""Tests for the market classifier."""
from __future__ import annotations

import pytest

from polyagent.models import MarketClass
from polyagent.services.classifier import classify


CRYPTO_QUESTIONS = [
    "Will Bitcoin reach $80,000 on April 22?",
    "Will ETH hit $5000 by May?",
    "Will Solana flip Ethereum in market cap this year?",
    "Will XRP be classified as a security in 2026?",
    "Will USDC maintain its peg through Q2?",
]

SPORTS_QUESTIONS = [
    "Madrid Open: Cristian Garin vs Alexander Blockx",
    "LoL: HANJIN BRION vs Dplus KIA (BO3) - LCK Rounds 1-2",
    "Chicago White Sox vs. Arizona Diamondbacks",
    "Ducks vs. Oilers",
    "Spread: FC Barcelona (-2.5)",
    "Will Burnley FC win on 2026-04-22?",
    "Dota 2: Xtreme Gaming vs Team Spirit (BO3) - PGL Wallachia Group Stage",
]

POLITICS_QUESTIONS = [
    "Will Trump be re-elected in 2028?",
    "Will the Senate pass the budget bill by June?",
    "Will Harris announce a presidential run?",
    "Will Congress impeach any sitting cabinet member in 2026?",
    "Who will win the 2026 Democratic primary in Ohio?",
]

MACRO_QUESTIONS = [
    "Will CPI YoY be above 3% in May?",
    "Will the Fed cut interest rates at the June FOMC meeting?",
    "Will US GDP growth exceed 2% in Q2 2026?",
    "Will unemployment hit 5% by year-end?",
    "Will the May jobs report show negative payrolls growth?",
]

OTHER_QUESTIONS = [
    "Will Taylor Swift attend the Super Bowl?",  # no sports keyword, should be OTHER
    "Will SpaceX launch Starship by Q3 2026?",
    "Will the new Marvel movie gross over $1B?",
    "",
    "   ",
]


@pytest.mark.parametrize("q", CRYPTO_QUESTIONS)
def test_classifies_crypto(q):
    assert classify(q, "unknown") == MarketClass.CRYPTO


@pytest.mark.parametrize("q", SPORTS_QUESTIONS)
def test_classifies_sports(q):
    assert classify(q, "unknown") == MarketClass.SPORTS


@pytest.mark.parametrize("q", POLITICS_QUESTIONS)
def test_classifies_politics(q):
    assert classify(q, "unknown") == MarketClass.POLITICS


@pytest.mark.parametrize("q", MACRO_QUESTIONS)
def test_classifies_macro(q):
    assert classify(q, "unknown") == MarketClass.MACRO


@pytest.mark.parametrize("q", OTHER_QUESTIONS)
def test_falls_back_to_other(q):
    assert classify(q, "unknown") == MarketClass.OTHER


def test_category_sports_overrides_question():
    # Ambiguous question, but Polymarket tagged it Sports -> trust category.
    assert classify("Will it happen?", "Sports") == MarketClass.SPORTS


def test_category_politics_overrides_question():
    assert classify("Will it happen?", "Politics") == MarketClass.POLITICS


def test_category_case_insensitive():
    assert classify("Will it happen?", "sports") == MarketClass.SPORTS
    assert classify("Will it happen?", "SPORTS") == MarketClass.SPORTS


def test_adversarial_tennessee_senate_is_politics_not_sports():
    # "Tennessee" superficially looks like a tennis keyword (it isn't, but we
    # want to be sure the Senate signal dominates).
    q = "Will the Tennessee Senate race be decided by September?"
    assert classify(q, "unknown") == MarketClass.POLITICS


def test_priority_crypto_beats_sports_keyword():
    # Adversarial: crypto question that happens to mention the NBA.
    q = "Will BTC sponsor the NBA Finals by July?"
    assert classify(q, "unknown") == MarketClass.CRYPTO


def test_priority_sports_beats_politics_keyword():
    # A politician at a game -> sports (the event is what we'd be trading).
    q = "Will Biden attend the Super Bowl?"
    assert classify(q, "unknown") == MarketClass.SPORTS


def test_never_raises_on_weird_input():
    # Pathological inputs should fall through to OTHER, not blow up.
    assert classify("???", "") == MarketClass.OTHER
    assert classify("\n\t", "\n") == MarketClass.OTHER
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/unit/test_classifier.py -v
```

Expected: ImportError (`polyagent.services.classifier` does not exist), all tests fail to collect.

- [ ] **Step 3: Implement the classifier**

Create `polyagent/services/classifier.py`:

```python
"""Coarse market classifier — tags each market with a MarketClass.

Pure function with no I/O, no side effects, never raises. The rule list is
ordered; the first class whose pattern list matches the question or category
wins. Default is MarketClass.OTHER.
"""
from __future__ import annotations

import re

from polyagent.models import MarketClass


# Patterns are compiled with re.IGNORECASE below. Word boundaries (\b) are
# used where the token could otherwise match inside another word.
CRYPTO_PATTERNS = [
    r"\bbitcoin\b",
    r"\bbtc\b",
    r"\bethereum\b",
    r"\beth\b",
    r"\bsolana\b",
    r"\bsol\b",
    r"\bxrp\b",
    r"\bdogecoin\b",
    r"\bdoge\b",
    r"\bcrypto\b",
    r"\bstablecoin\b",
    r"\busdc\b",
    r"\busdt\b",
]

SPORTS_PATTERNS = [
    r" vs\. ",
    r" vs ",
    r" Open:",
    r"\bBO3\b",
    r"\bBO5\b",
    r"Spread:",
    r"\bMLB\b",
    r"\bNBA\b",
    r"\bNHL\b",
    r"\bNFL\b",
    r"\bNCAA[MF]?\b",
    r"\bUEFA\b",
    r"\bLCK\b",
    r"\bLoL\b",
    r"\bDota\b",
    r"\bCS2\b",
    r"\bValorant\b",
    r"Premier League",
    r"La Liga",
    r"Bundesliga",
    r"Serie A",
    r"Super Bowl",
    r"^Will .+ win on \d{4}-\d{2}-\d{2}\??$",
]

POLITICS_PATTERNS = [
    r"\bpresident(ial)?\b",
    r"\belection\b",
    r"\bprimary\b",
    r"\bSenate\b",
    r"\bCongress\b",
    r"Supreme Court",
    r"\bTrump\b",
    r"\bBiden\b",
    r"\bHarris\b",
    r"\bVance\b",
    r"\bgovernor\b",
    r"\bimpeach(ment)?\b",
    r"\bcabinet\b",
]

MACRO_PATTERNS = [
    r"\bCPI\b",
    r"\binflation\b",
    r"\bFed\b",
    r"\bFOMC\b",
    r"interest rate",
    r"\brecession\b",
    r"\bGDP\b",
    r"\bunemployment\b",
    r"jobs report",
    r"\bpayrolls?\b",
]


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


CLASS_RULES: list[tuple[MarketClass, list[re.Pattern[str]]]] = [
    (MarketClass.CRYPTO, _compile(CRYPTO_PATTERNS)),
    (MarketClass.SPORTS, _compile(SPORTS_PATTERNS)),
    (MarketClass.POLITICS, _compile(POLITICS_PATTERNS)),
    (MarketClass.MACRO, _compile(MACRO_PATTERNS)),
]


# Polymarket category string -> MarketClass (case-insensitive match).
# If Gamma tags a market definitively we trust it over the question scan,
# but only for classes where the mapping is unambiguous.
CATEGORY_MAP: dict[str, MarketClass] = {
    "sports": MarketClass.SPORTS,
    "politics": MarketClass.POLITICS,
    "crypto": MarketClass.CRYPTO,
}


def classify(question: str, category: str) -> MarketClass:
    """Return the coarse class of a market. Pure, never raises.

    Order of evaluation:
      1. Polymarket's own `category` tag (if it's one we trust).
      2. The question-text rule cascade.
      3. OTHER.

    Args:
        question: The market question text (may be empty).
        category: The Polymarket category string (may be empty or 'unknown').

    Returns:
        The first MarketClass whose rules match, or MarketClass.OTHER.
    """
    cat_norm = (category or "").strip().lower()
    if cat_norm in CATEGORY_MAP:
        return CATEGORY_MAP[cat_norm]

    q = question or ""
    for cls, patterns in CLASS_RULES:
        for p in patterns:
            if p.search(q):
                return cls

    return MarketClass.OTHER
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/unit/test_classifier.py -v
```

Expected: all tests pass. If any fail, adjust patterns — do *not* weaken assertions.

- [ ] **Step 5: Commit**

```bash
git add polyagent/services/classifier.py tests/unit/test_classifier.py
git commit -m "feat(classifier): add rule-based market classifier"
```

---

## Task 4: Repository persists `market_class`

**Files:**
- Modify: `polyagent/data/repositories/markets.py`
- Modify: `tests/unit/test_market_repo.py`

- [ ] **Step 1: Write failing test that asserts `market_class` is passed to SQL**

Append to `tests/unit/test_market_repo.py` (inside `TestMarketRepository`):

```python
    def test_upsert_passes_market_class_to_sql(self):
        from polyagent.models import MarketClass

        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = {"id": uuid4()}
        self.db.cursor.return_value = mock_cursor

        market = MarketData(
            polymarket_id="0xabc",
            question="Will BTC hit 150k?",
            category="crypto",
            token_id="tok_1",
            midpoint_price=Decimal("0.45"),
            bids_depth=Decimal("2000"),
            asks_depth=Decimal("1800"),
            hours_to_resolution=48.0,
            volume_24h=Decimal("150000"),
            market_class=MarketClass.CRYPTO,
        )
        self.repo.upsert(market)

        args, kwargs = mock_cursor.execute.call_args
        # psycopg call shape: execute(sql, params_dict)
        params = args[1] if len(args) > 1 else kwargs.get("params") or kwargs
        assert params["market_class"] == "crypto"

    def test_upsert_defaults_market_class_when_unset(self):
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = {"id": uuid4()}
        self.db.cursor.return_value = mock_cursor

        market = MarketData(
            polymarket_id="0xabc",
            question="q",
            category="unknown",
            token_id="tok",
            midpoint_price=Decimal("0.5"),
            bids_depth=Decimal("100"),
            asks_depth=Decimal("100"),
            hours_to_resolution=10.0,
            volume_24h=Decimal("0"),
            # market_class omitted
        )
        self.repo.upsert(market)

        args, kwargs = mock_cursor.execute.call_args
        params = args[1] if len(args) > 1 else kwargs.get("params") or kwargs
        assert params["market_class"] == "other"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/unit/test_market_repo.py -v
```

Expected: the two new tests fail with `KeyError: 'market_class'` (SQL params dict doesn't contain the key yet). Existing tests still pass.

- [ ] **Step 3: Update `UPSERT_MARKET` SQL and pass `market_class`**

In `polyagent/data/repositories/markets.py`, replace the `UPSERT_MARKET` constant and extend `upsert()`:

```python
from polyagent.models import MarketClass, MarketData, MarketStatus, Score

UPSERT_MARKET = """
    INSERT INTO markets (
        polymarket_id, question, category, token_id,
        midpoint_price, bids_depth, asks_depth,
        hours_to_resolution, volume_24h, status, market_class
    ) VALUES (
        %(polymarket_id)s, %(question)s, %(category)s, %(token_id)s,
        %(midpoint_price)s, %(bids_depth)s, %(asks_depth)s,
        %(hours_to_resolution)s, %(volume_24h)s, %(status)s, %(market_class)s
    )
    ON CONFLICT (polymarket_id) DO UPDATE SET
        midpoint_price = EXCLUDED.midpoint_price,
        bids_depth = EXCLUDED.bids_depth,
        asks_depth = EXCLUDED.asks_depth,
        hours_to_resolution = EXCLUDED.hours_to_resolution,
        volume_24h = EXCLUDED.volume_24h,
        market_class = EXCLUDED.market_class,
        scanned_at = NOW()
    RETURNING id
"""
```

And update the `upsert` method's params dict to include the new key (use the value from `market.market_class`, falling back to `MarketClass.OTHER` when unset):

```python
    def upsert(self, market: MarketData, status: MarketStatus = MarketStatus.QUEUED) -> UUID:
        """Insert or update a market, return its UUID."""
        market_class = (market.market_class or MarketClass.OTHER).value
        with self._db.cursor() as cur:
            cur.execute(
                UPSERT_MARKET,
                {
                    "polymarket_id": market.polymarket_id,
                    "question": market.question,
                    "category": market.category,
                    "token_id": market.token_id,
                    "midpoint_price": market.midpoint_price,
                    "bids_depth": market.bids_depth,
                    "asks_depth": market.asks_depth,
                    "hours_to_resolution": market.hours_to_resolution,
                    "volume_24h": market.volume_24h,
                    "status": status.value,
                    "market_class": market_class,
                },
            )
            row = cur.fetchone()
            return row["id"]
```

- [ ] **Step 4: Run all repo tests**

Run:

```bash
uv run pytest tests/unit/test_market_repo.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add polyagent/data/repositories/markets.py tests/unit/test_market_repo.py
git commit -m "feat(repo): persist market_class on upsert"
```

---

## Task 5: Scanner wiring in `main.py`

**Files:**
- Modify: `polyagent/main.py`

This task has no new unit test — the behavior is one line of wiring covered by the integration test in Task 6 and the classifier's own tests. The goal is that every market upserted by the scanner has its `market_class` set by the classifier.

- [ ] **Step 1: Add import**

At the top of `polyagent/main.py`, with the other service imports:

```python
from polyagent.services.classifier import classify
```

- [ ] **Step 2: Set `market_class` before upsert inside `scanner_worker`**

Locate this block inside `scanner_worker`:

```python
                for market, score in survivors:
                    db_id = market_repo.upsert(market, MarketStatus.QUEUED)
                    if db_id in open_market_ids:
                        skipped_open += 1
                        continue
                    if db_id in cooldown_market_ids:
                        skipped_cooldown += 1
                        continue
                    market_repo.update_score(db_id, score, MarketStatus.QUEUED)
                    queues.scan_queue.put(ScanResult(market=market, market_db_id=db_id, score=score))
```

Replace with (adds one line before `upsert`):

```python
                for market, score in survivors:
                    market.market_class = classify(market.question, market.category)
                    db_id = market_repo.upsert(market, MarketStatus.QUEUED)
                    if db_id in open_market_ids:
                        skipped_open += 1
                        continue
                    if db_id in cooldown_market_ids:
                        skipped_cooldown += 1
                        continue
                    market_repo.update_score(db_id, score, MarketStatus.QUEUED)
                    queues.scan_queue.put(ScanResult(market=market, market_db_id=db_id, score=score))
```

- [ ] **Step 3: Run the full unit test suite to confirm nothing regressed**

Run:

```bash
uv run pytest tests/unit -v
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add polyagent/main.py
git commit -m "feat(scanner): classify markets before upsert"
```

---

## Task 6: `polyagent class-stats` CLI command

**Files:**
- Create: `polyagent/cli/class_stats.py`
- Modify: `polyagent/cli/main.py`
- Create: `tests/integration/test_class_stats_cli.py`

- [ ] **Step 1: Write failing integration test**

Create `tests/integration/test_class_stats_cli.py`:

```python
"""End-to-end test for `polyagent class-stats` against a real DB.

Requires --run-integration and a running polyagent_test database matching
the schema in db/migrations/. This test seeds markets+positions directly,
invokes the CLI, and asserts aggregates in the rendered output.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from click.testing import CliRunner

from polyagent.cli.class_stats import class_stats
from polyagent.infra.config import Settings
from polyagent.infra.database import Database


pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_db(settings: Settings):
    db = Database(settings)
    with db.cursor() as cur:
        cur.execute("DELETE FROM trade_log")
        cur.execute("DELETE FROM positions")
        cur.execute("DELETE FROM thesis")
        cur.execute("DELETE FROM markets")

        sports_id = uuid4()
        crypto_id = uuid4()
        cur.execute(
            """
            INSERT INTO markets (id, polymarket_id, question, category, token_id,
                                 midpoint_price, bids_depth, asks_depth,
                                 hours_to_resolution, volume_24h, status, market_class)
            VALUES
                (%s, '0xs', 'Team A vs. Team B', 'Sports', 't1', 0.5, 100, 100, 24, 0, 'traded', 'sports'),
                (%s, '0xc', 'Will BTC hit $80,000?', 'Crypto', 't2', 0.5, 100, 100, 24, 0, 'traded', 'crypto')
            """,
            (sports_id, crypto_id),
        )

        thesis_s = uuid4()
        thesis_c = uuid4()
        cur.execute(
            """
            INSERT INTO thesis (id, market_id, claude_estimate, confidence, checks,
                                checks_passed, thesis_text, strategy_votes, consensus)
            VALUES
                (%s, %s, 0.5, 0.8, '{}'::jsonb, 0, '', '{}'::jsonb, 'full'),
                (%s, %s, 0.5, 0.8, '{}'::jsonb, 0, '', '{}'::jsonb, 'full')
            """,
            (thesis_s, sports_id, thesis_c, crypto_id),
        )

        opened = datetime.now(timezone.utc) - timedelta(hours=10)
        closed = datetime.now(timezone.utc)
        cur.execute(
            """
            INSERT INTO positions (id, thesis_id, market_id, side, entry_price,
                                   target_price, kelly_fraction, position_size,
                                   current_price, status, exit_reason, pnl,
                                   paper_trade, opened_at, closed_at)
            VALUES
                (%s, %s, %s, 'BUY', 0.3, 0.7, 0.1, 10.0, 0.0, 'closed', 'RESOLVED_NO', -1.00, true, %s, %s),
                (%s, %s, %s, 'SELL', 0.5, 0.1, 0.1, 10.0, 0.0, 'closed', 'RESOLVED_NO',  0.50, true, %s, %s),
                (%s, %s, %s, 'BUY', 0.2, 0.6, 0.1, 10.0, 0.0, 'closed', 'RESOLVED_NO', -0.25, true, %s, %s)
            """,
            (
                uuid4(), thesis_s, sports_id, opened, closed,
                uuid4(), thesis_s, sports_id, opened, closed,
                uuid4(), thesis_c, crypto_id, opened, closed,
            ),
        )
    yield db
    db.close()


def test_class_stats_reports_per_class_aggregates(seeded_db):
    runner = CliRunner()
    result = runner.invoke(class_stats, [])
    assert result.exit_code == 0, result.output
    # Sports: 2 trades, 1 win, 1 loss, net = -1.00 + 0.50 = -0.50
    assert "sports" in result.output
    assert "2" in result.output  # sports trades
    # Crypto: 1 trade, 0 wins, 1 loss, net = -0.25
    assert "crypto" in result.output
    # Totals row
    assert "TOTAL" in result.output or "Total" in result.output
```

- [ ] **Step 2: Run test to confirm it fails**

Run:

```bash
uv run pytest tests/integration/test_class_stats_cli.py --run-integration -v
```

Expected: `ModuleNotFoundError: polyagent.cli.class_stats` — the module doesn't exist yet.

- [ ] **Step 3: Implement the CLI command**

Create `polyagent/cli/class_stats.py`:

```python
"""Per-class performance analytics — `polyagent class-stats`."""
from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

from polyagent.infra.config import Settings
from polyagent.infra.database import Database


CLASS_STATS_QUERY = """
    SELECT
        m.market_class::text AS class,
        COUNT(*) FILTER (WHERE p.status = 'closed')                 AS trades,
        COUNT(*) FILTER (WHERE p.status = 'closed' AND p.pnl > 0)   AS wins,
        COUNT(*) FILTER (WHERE p.status = 'closed' AND p.pnl <= 0)  AS losses,
        COALESCE(SUM(p.pnl)  FILTER (WHERE p.status = 'closed'), 0) AS total_pnl,
        COALESCE(AVG(p.pnl)  FILTER (WHERE p.status = 'closed'), 0) AS avg_pnl,
        COALESCE(
            AVG(EXTRACT(EPOCH FROM (p.closed_at - p.opened_at)) / 3600)
            FILTER (WHERE p.status = 'closed'), 0
        )                                                           AS avg_hold_h
    FROM markets m
    LEFT JOIN positions p ON p.market_id = m.id
    GROUP BY m.market_class
    ORDER BY total_pnl DESC
"""


@click.command("class-stats")
def class_stats():
    """Show per-class performance: trades, win rate, P&L, hold time."""
    console = Console()
    settings = Settings.from_env()
    db = Database(settings)

    with db.cursor() as cur:
        cur.execute(CLASS_STATS_QUERY)
        rows = cur.fetchall()

    table = Table(title="Per-Class Performance")
    table.add_column("Class", style="cyan")
    table.add_column("Trades", justify="right")
    table.add_column("W/L", justify="right")
    table.add_column("Win%", justify="right")
    table.add_column("Avg P&L", justify="right")
    table.add_column("Total P&L", justify="right")
    table.add_column("Avg Hold", justify="right")

    total_trades = total_wins = total_losses = 0
    grand_total_pnl = 0.0

    for r in rows:
        trades = int(r["trades"] or 0)
        wins = int(r["wins"] or 0)
        losses = int(r["losses"] or 0)
        total_pnl = float(r["total_pnl"] or 0)
        avg_pnl = float(r["avg_pnl"] or 0)
        avg_hold = float(r["avg_hold_h"] or 0)

        total_trades += trades
        total_wins += wins
        total_losses += losses
        grand_total_pnl += total_pnl

        if trades == 0:
            table.add_row(r["class"], "0", "-", "-", "-", "$0.00", "-")
            continue

        win_pct = (wins / trades) * 100
        pnl_style = "green" if total_pnl >= 0 else "red"
        avg_style = "green" if avg_pnl >= 0 else "red"
        table.add_row(
            r["class"],
            str(trades),
            f"{wins}/{losses}",
            f"{win_pct:.0f}%",
            f"[{avg_style}]${avg_pnl:+,.2f}[/{avg_style}]",
            f"[{pnl_style}]${total_pnl:+,.2f}[/{pnl_style}]",
            f"{avg_hold:.0f}h",
        )

    if total_trades:
        total_win_pct = (total_wins / total_trades) * 100
        grand_avg = grand_total_pnl / total_trades
        grand_style = "green" if grand_total_pnl >= 0 else "red"
        avg_style = "green" if grand_avg >= 0 else "red"
        table.add_section()
        table.add_row(
            "TOTAL",
            str(total_trades),
            f"{total_wins}/{total_losses}",
            f"{total_win_pct:.0f}%",
            f"[{avg_style}]${grand_avg:+,.2f}[/{avg_style}]",
            f"[{grand_style}]${grand_total_pnl:+,.2f}[/{grand_style}]",
            "-",
        )

    console.print(table)
    db.close()
```

- [ ] **Step 4: Register the command**

In `polyagent/cli/main.py`, add the import and the `add_command` call:

```python
from polyagent.cli.class_stats import class_stats
```

And at the bottom with the other `cli.add_command(...)` lines:

```python
cli.add_command(class_stats)
```

- [ ] **Step 5: Run the integration test**

Run:

```bash
uv run pytest tests/integration/test_class_stats_cli.py --run-integration -v
```

Expected: the test passes.

- [ ] **Step 6: Smoke-test the CLI against the real DB**

Run:

```bash
uv run polyagent class-stats
```

Expected: a table with one row per class. Classes with no positions show dashes. No crashes.

- [ ] **Step 7: Commit**

```bash
git add polyagent/cli/class_stats.py polyagent/cli/main.py tests/integration/test_class_stats_cli.py
git commit -m "feat(cli): add class-stats per-class performance command"
```

---

## Task 7: Backfill script

**Files:**
- Create: `polyagent/scripts/backfill_market_class.py`
- Create: `tests/integration/test_backfill_market_class.py`

- [ ] **Step 1: Write failing integration test**

Create `tests/integration/test_backfill_market_class.py`:

```python
"""End-to-end test for the market_class backfill script."""
from __future__ import annotations

from uuid import uuid4

import pytest

from polyagent.infra.config import Settings
from polyagent.infra.database import Database
from polyagent.scripts.backfill_market_class import backfill


pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_db(settings: Settings):
    db = Database(settings)
    with db.cursor() as cur:
        cur.execute("DELETE FROM trade_log")
        cur.execute("DELETE FROM positions")
        cur.execute("DELETE FROM thesis")
        cur.execute("DELETE FROM markets")

        # All rows inserted with the default market_class ('other').
        cur.execute(
            """
            INSERT INTO markets (polymarket_id, question, category, token_id,
                                 midpoint_price, bids_depth, asks_depth,
                                 hours_to_resolution, volume_24h, status)
            VALUES
                ('0x1', 'Madrid Open: A vs B', 'Sports',   't1', 0.5, 100, 100, 24, 0, 'queued'),
                ('0x2', 'Will BTC hit $80,000?', 'Crypto',  't2', 0.5, 100, 100, 24, 0, 'queued'),
                ('0x3', 'Will Trump win re-election?', 'Politics', 't3', 0.5, 100, 100, 24, 0, 'queued'),
                ('0x4', 'Will CPI YoY be above 3%?', 'Economics', 't4', 0.5, 100, 100, 24, 0, 'queued'),
                ('0x5', 'Will SpaceX launch by Q3?', 'Tech',  't5', 0.5, 100, 100, 24, 0, 'queued')
            """
        )
    yield db
    db.close()


def test_backfill_assigns_correct_class(seeded_db):
    counts = backfill(seeded_db)
    assert counts["sports"] == 1
    assert counts["crypto"] == 1
    assert counts["politics"] == 1
    assert counts["macro"] == 1
    assert counts["other"] == 1

    with seeded_db.cursor() as cur:
        cur.execute(
            "SELECT polymarket_id, market_class::text FROM markets ORDER BY polymarket_id"
        )
        rows = cur.fetchall()

    result = {r["polymarket_id"]: r["market_class"] for r in rows}
    assert result == {
        "0x1": "sports",
        "0x2": "crypto",
        "0x3": "politics",
        "0x4": "macro",
        "0x5": "other",
    }


def test_backfill_is_idempotent(seeded_db):
    backfill(seeded_db)
    counts2 = backfill(seeded_db)
    assert counts2["sports"] == 1
    assert counts2["crypto"] == 1
    assert counts2["politics"] == 1
    assert counts2["macro"] == 1
    assert counts2["other"] == 1
```

- [ ] **Step 2: Run test to confirm it fails**

Run:

```bash
uv run pytest tests/integration/test_backfill_market_class.py --run-integration -v
```

Expected: `ModuleNotFoundError: polyagent.scripts.backfill_market_class`.

- [ ] **Step 3: Implement the backfill script**

Create `polyagent/scripts/backfill_market_class.py`:

```python
"""One-shot backfill for markets.market_class.

Reads every row from `markets`, applies the classifier, and updates the
market_class column. Idempotent — safe to re-run after classifier tweaks.
Commits per row so a transient error on one row does not roll back work
already persisted.

Usage:
    python -m polyagent.scripts.backfill_market_class
"""
from __future__ import annotations

import logging
import sys
from collections import Counter

from polyagent.infra.config import Settings
from polyagent.infra.database import Database
from polyagent.infra.logging import setup_logging
from polyagent.services.classifier import classify

logger = logging.getLogger("polyagent.scripts.backfill_market_class")


def backfill(db: Database) -> Counter[str]:
    """Classify every market row and persist the result.

    Returns a Counter keyed by MarketClass value (e.g. {"sports": 18, ...}).
    """
    counts: Counter[str] = Counter()

    with db.cursor() as cur:
        cur.execute("SELECT id, question, category FROM markets")
        rows = cur.fetchall()

    for row in rows:
        try:
            cls = classify(row["question"] or "", row["category"] or "")
        except Exception:
            logger.exception("classify failed for market %s", row["id"])
            continue

        try:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE markets SET market_class = %s WHERE id = %s",
                    (cls.value, row["id"]),
                )
        except Exception:
            logger.exception("UPDATE failed for market %s", row["id"])
            continue

        counts[cls.value] += 1

    logger.info("backfill complete: %s", dict(counts))
    return counts


def main() -> int:
    setup_logging()
    settings = Settings.from_env()
    db = Database(settings)
    try:
        backfill(db)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the integration tests**

Run:

```bash
uv run pytest tests/integration/test_backfill_market_class.py --run-integration -v
```

Expected: both tests pass.

- [ ] **Step 5: Commit**

```bash
git add polyagent/scripts/backfill_market_class.py tests/integration/test_backfill_market_class.py
git commit -m "feat(scripts): backfill market_class for existing rows"
```

---

## Task 8: Rollout — apply migration, run backfill, verify

This task is operational, not code. All previous tasks have been committed. This task brings the running system up to date.

- [ ] **Step 1: Apply the migration to the running DB** (already done in Task 1 for dev — repeat here if deploying to a different environment)

Run:

```bash
podman exec -i polyagent-db psql -U polyagent -d polyagent < db/migrations/004_market_class.sql
```

Expected: no errors.

- [ ] **Step 2: Run the backfill**

Run:

```bash
uv run python -m polyagent.scripts.backfill_market_class
```

Expected output ends with a log line like:

```
backfill complete: {'sports': 18, 'crypto': 1, 'politics': 0, 'macro': 0, 'other': 0}
```

(Exact numbers depend on your market history.)

- [ ] **Step 3: Verify `class-stats` reports historical trades**

Run:

```bash
uv run polyagent class-stats
```

Expected: the `sports` and `crypto` rows have non-zero trade counts and reflect your historical paper-trade P&L. `politics`, `macro`, `other` show dashes if empty.

- [ ] **Step 4: Restart the bot so the scanner picks up the new wiring**

Run (if applicable to your deployment):

```bash
podman compose -f compose.yaml restart polyagent
```

Or whatever your usual restart incantation is. New scan cycles will now classify markets as they come in.

- [ ] **Step 5: Push**

```bash
git push
```

---

## Self-Review Notes

Spec coverage check — each spec section maps to at least one task:

- Motivation / Goals / Non-Goals — Tasks 1-8 implement the stated scope; nothing blocking added.
- Architecture diagram — Tasks 3 (classifier), 4 (repo), 5 (scanner wiring), 6 (CLI).
- Data Model (enum, `MarketData` field, migration) — Tasks 1, 2.
- Retroactive backfill — Task 7.
- Classifier Rules & Public API — Task 3.
- Wiring (scanner + repository) — Tasks 4, 5.
- CLI — Task 6.
- Error Handling — classifier tests cover never-raises (Task 3); backfill tests cover idempotency (Task 7); CLI handles empty result (code path in Task 6).
- Testing — unit tests in Tasks 2, 3, 4; integration tests in Tasks 6, 7.
- Rollout — Task 8.
- Open Questions / Follow-Up — deliberately out of scope, no tasks.

No placeholder strings (`TBD`, `TODO`, `similar to`, etc.) in any task. All types/signatures used in later tasks (`MarketClass`, `classify`, `CLASS_STATS_QUERY`, `backfill`) are defined in earlier tasks in this file.

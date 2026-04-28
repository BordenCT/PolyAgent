# Multi-Asset Quant Subsystem Refactor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify `polyagent/services/btc5m/` and `polyagent/services/crypto_quant/` into a single `polyagent/services/quant/` package backed by a typed `AssetSpec` registry, with a hand-rolled migration runner so prod schema cannot drift.

**Architecture:** Six independently-mergeable PRs. Prod (BTC + ETH) stays green throughout. Each PR is decomposed into bite-sized TDD tasks: failing test → verify fail → minimal impl → verify pass → commit. Re-exports preserve old import paths during the transitional PRs and are removed in PR 6.

**Tech Stack:** Python 3.14, psycopg[binary,pool] 3.2, click 8.1, httpx 0.27, pytest 8 (with `integration` marker for DB-touching tests). No new runtime dependencies.

**Spec:** `docs/refactor/quant-multi-asset.md` (commit `57423d2`).

---

## File Structure Map

### New files (created across PRs)

| File | Responsibility | First introduced in |
|---|---|---|
| `polyagent/scripts/__init__.py` | package marker | PR 1 |
| `polyagent/scripts/migrate.py` | migration runner core (apply/status/baseline) | PR 1 |
| `polyagent/cli/migrate_cmd.py` | click subcommand wrapping `scripts/migrate.py` | PR 1 |
| `tests/unit/test_migrate_runner.py` | unit tests for runner internals (checksum, version parse) | PR 1 |
| `tests/integration/test_migrate_runner.py` | integration tests against ephemeral Postgres | PR 1 |
| `polyagent/services/quant/__init__.py` | package marker | PR 2 |
| `polyagent/services/quant/core/__init__.py` | re-exports estimator + vol + pnl | PR 2 |
| `polyagent/services/quant/core/estimator.py` | Φ(d₂) — moved from `btc5m/estimator.py` | PR 2 |
| `polyagent/services/quant/core/pnl.py` | binary-option P&L — moved from `btc5m/resolver.py` | PR 2 |
| `polyagent/services/quant/core/vol.py` | `compute_vol(spec, source, horizon_s)` + `VolCalibration` + `VolMethod` | PR 2 |
| `tests/unit/test_quant_core_vol.py` | vol calibration policies | PR 2 |
| `polyagent/services/quant/assets/__init__.py` | re-exports spec + registry | PR 3 |
| `polyagent/services/quant/assets/spec.py` | `AssetSpec`, `AssetClass`, `MarketFamily` | PR 3 |
| `polyagent/services/quant/assets/registry.py` | `ASSETS` dict + `get`/`enabled_for`/`live_eligible`/`apply_env_overrides` | PR 3 |
| `polyagent/services/quant/assets/sources/__init__.py` | re-exports source classes | PR 3 |
| `polyagent/services/quant/assets/sources/base.py` | `PriceSource`, `SettlementSource` Protocols | PR 3 |
| `polyagent/services/quant/assets/sources/coinbase.py` | `CoinbaseSpotSource` (PriceSource + SettlementSource) | PR 3 |
| `tests/unit/test_quant_assets_registry.py` | registry read API + env overrides | PR 3 |
| `tests/unit/test_quant_assets_sources_coinbase.py` | source against fake httpx | PR 3 |
| `polyagent/services/quant/strike/__init__.py` | re-exports `QuantStrikeService` | PR 4 |
| `polyagent/services/quant/strike/parser.py` | registry-driven question parser | PR 4 |
| `polyagent/services/quant/strike/service.py` | `QuantStrikeService` | PR 4 |
| `tests/unit/test_quant_strike_parser.py` | parser fixtures per asset | PR 4 |
| `tests/unit/test_quant_strike_service.py` | service against fake source registry | PR 4 |
| `db/migrations/006_quant_short_rename.sql` | rename btc5m_* tables, add asset_id + price_source_id | PR 5 |
| `polyagent/services/quant/short_horizon/__init__.py` | re-exports | PR 6 |
| `polyagent/services/quant/short_horizon/scanner.py` | registry-aware slug scanner | PR 6 |
| `polyagent/services/quant/short_horizon/decider.py` | registry-aware decider | PR 6 |
| `polyagent/services/quant/short_horizon/resolver.py` | resolver writing audit field | PR 6 |
| `polyagent/services/quant/short_horizon/repository.py` | renamed `Btc5mRepository` → `QuantShortRepository` | PR 5 (see note) |
| `polyagent/services/quant/orchestrator.py` | single worker thread | PR 6 |
| `polyagent/services/quant/cli/stats.py` | generalizes `btc5m-stats` → `quant-stats` | PR 6 |
| `tests/unit/test_quant_short_*` | per-component unit tests | PR 6 |
| `tests/integration/test_quant_orchestrator.py` | end-to-end with fake registry + in-memory repo | PR 6 |

> **Note on PR 5 vs PR 6:** PR 5 introduces `polyagent/services/quant/short_horizon/repository.py` (the renamed repository class) so the table rename + repo rename ship together. PR 6 introduces the rest of the `short_horizon/` modules and replaces the worker.

### Modified files

| File | Change | PR |
|---|---|---|
| `pyproject.toml` | add `polyagent migrate` to scripts (already a click subcommand, so no change needed) | — |
| `polyagent/cli/main.py` | register `migrate` subcommand (PR 1); register `quant_stats` and unregister `btc5m_stats` (PR 6) | PR 1, PR 6 |
| `polyagent/services/btc5m/estimator.py` | replace body with re-export from `quant.core.estimator` | PR 2 |
| `polyagent/services/btc5m/resolver.py` | move `compute_pnl` to `quant.core.pnl`, leave re-export; update vol calls | PR 2 |
| `polyagent/services/btc5m/decider.py` | switch vol calls to `compute_vol` (still asset-hardcoded for BTC) | PR 2 |
| `polyagent/services/btc5m/spot.py` | replace body with re-export from `quant.assets.sources.coinbase` | PR 3 |
| `polyagent/services/crypto_quant/__init__.py` | replace with re-export from `quant.strike` | PR 4 |
| `polyagent/services/crypto_quant/service.py` | DELETED | PR 4 |
| `polyagent/services/crypto_quant/parser.py` | DELETED | PR 4 |
| `polyagent/services/crypto_quant/estimator.py` | DELETED | PR 4 |
| `polyagent/services/brain.py` | one-line import: `crypto_quant` → `quant.strike` | PR 4 |
| `polyagent/data/repositories/btc5m.py` | rename class `Btc5mRepository` → `QuantShortRepository`; leave alias | PR 5 |
| `polyagent/models.py` | rename `Btc5mMarket`/`Btc5mTrade` → `QuantShortMarket`/`QuantShortTrade`; leave aliases | PR 5 |
| `polyagent/services/btc5m/worker.py` | DELETED | PR 6 |
| `polyagent/services/btc5m/scanner.py` | DELETED | PR 6 |
| `polyagent/services/btc5m/decider.py` | DELETED | PR 6 |
| `polyagent/services/btc5m/resolver.py` | DELETED | PR 6 |
| `polyagent/services/btc5m/spot.py` | DELETED | PR 6 |
| `polyagent/services/btc5m/estimator.py` | DELETED | PR 6 |
| `polyagent/services/btc5m/__init__.py` | DELETED | PR 6 |
| `polyagent/services/crypto_quant/__init__.py` | DELETED | PR 6 |
| `polyagent/main.py` | replace `run_btc5m_worker` invocation with `run_quant_orchestrator` | PR 6 |
| `polyagent/infra/config.py` | `BTC5M_*` → `QUANT_SHORT_*`; `CRYPTO_QUANT_*` → folded into registry; add `QUANT_<ASSET>_*` overrides | PR 6 |
| `.env.example` | rename env keys | PR 6 |

---

# PR 1 — Migration Runner

**Goal:** Add `polyagent migrate {up,status,baseline}` CLI backed by `polyagent/scripts/migrate.py`. Apply pending `db/migrations/*.sql` in lex order, each in its own transaction. Detect checksum drift on already-applied files. Bootstrap `schema_migrations` table on first invocation.

**Branch:** `refactor/quant-pr1-migrate-runner`

### Task 1.1: Schema_migrations table + ensure_table

**Files:**
- Create: `polyagent/scripts/__init__.py` (empty)
- Create: `polyagent/scripts/migrate.py`
- Create: `tests/integration/test_migrate_runner.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_migrate_runner.py
import pytest
import psycopg

from polyagent.scripts.migrate import ensure_schema_migrations_table


@pytest.mark.integration
def test_ensure_schema_migrations_table_creates_table_when_absent(empty_db_url):
    with psycopg.connect(empty_db_url) as conn:
        ensure_schema_migrations_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'schema_migrations' ORDER BY ordinal_position"
            )
            cols = [r[0] for r in cur.fetchall()]
    assert cols == ["version", "filename", "checksum", "applied_at"]


@pytest.mark.integration
def test_ensure_schema_migrations_table_is_idempotent(empty_db_url):
    with psycopg.connect(empty_db_url) as conn:
        ensure_schema_migrations_table(conn)
        ensure_schema_migrations_table(conn)  # second call must not error
```

The `empty_db_url` fixture is added in Task 1.2.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_migrate_runner.py -v -m integration`
Expected: FAIL — `polyagent.scripts.migrate` import error or `empty_db_url` fixture missing.

- [ ] **Step 3: Write minimal implementation**

```python
# polyagent/scripts/migrate.py
"""Hand-rolled SQL migration runner.

Reads `db/migrations/*.sql`, applies any not yet recorded in
`schema_migrations` in a transaction each, detects checksum drift on
already-applied files.
"""
from __future__ import annotations

import psycopg

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def ensure_schema_migrations_table(conn: psycopg.Connection) -> None:
    """Create the schema_migrations table if it does not exist."""
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_MIGRATIONS_DDL)
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/test_migrate_runner.py -v -m integration`
Expected: PASS (after Task 1.2 adds the fixture).

- [ ] **Step 5: Commit**

```bash
git add polyagent/scripts/__init__.py polyagent/scripts/migrate.py tests/integration/test_migrate_runner.py
git commit -m "feat(migrate): add ensure_schema_migrations_table"
```

### Task 1.2: Empty-DB pytest fixture

**Files:**
- Modify: `tests/conftest.py`

- [ ] **Step 1: Write the failing test (re-use Task 1.1 tests)**

The tests from Task 1.1 require an `empty_db_url` fixture providing a clean Postgres URL.

- [ ] **Step 2: Verify failure mode**

Run: `pytest tests/integration/test_migrate_runner.py -v -m integration`
Expected: ERROR — `fixture 'empty_db_url' not found`.

- [ ] **Step 3: Add the fixture**

```python
# tests/conftest.py — add to existing file
import os
import uuid
import pytest
import psycopg


@pytest.fixture
def empty_db_url():
    """Yield a connection URL to a fresh empty database; drop on teardown.

    Requires POLYAGENT_TEST_DB_URL env var pointing at a Postgres superuser
    URL on a server we can CREATE/DROP DATABASE against.
    """
    base = os.environ.get("POLYAGENT_TEST_DB_URL")
    if not base:
        pytest.skip("POLYAGENT_TEST_DB_URL not set")
    db_name = f"polyagent_test_{uuid.uuid4().hex[:12]}"
    admin = psycopg.connect(base, autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{db_name}"')
        target_url = base.rsplit("/", 1)[0] + f"/{db_name}"
        with psycopg.connect(target_url) as conn:
            with conn.cursor() as cur:
                cur.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
            conn.commit()
        yield target_url
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        admin.close()
```

- [ ] **Step 4: Run tests to verify pass**

Run: `POLYAGENT_TEST_DB_URL=postgresql://polyagent:polyagent@localhost:5432/postgres pytest tests/integration/test_migrate_runner.py -v -m integration`
Expected: PASS for both Task 1.1 tests.

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py
git commit -m "test(migrate): add empty_db_url fixture"
```

### Task 1.3: Discover migration files

**Files:**
- Modify: `polyagent/scripts/migrate.py`
- Create: `tests/unit/test_migrate_runner.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_migrate_runner.py
import hashlib
from pathlib import Path

from polyagent.scripts.migrate import (
    Migration,
    discover_migrations,
)


def test_discover_migrations_returns_lex_sorted_with_versions(tmp_path):
    (tmp_path / "002_second.sql").write_text("SELECT 2;")
    (tmp_path / "001_first.sql").write_text("SELECT 1;")
    (tmp_path / "010_tenth.sql").write_text("SELECT 10;")
    (tmp_path / "README.md").write_text("ignored")  # non-sql ignored

    found = discover_migrations(tmp_path)

    assert [m.version for m in found] == ["001", "002", "010"]
    assert [m.filename for m in found] == [
        "001_first.sql", "002_second.sql", "010_tenth.sql",
    ]
    assert found[0].sql == "SELECT 1;"
    assert found[0].checksum == hashlib.sha256(b"SELECT 1;").hexdigest()


def test_discover_migrations_empty_dir_returns_empty(tmp_path):
    assert discover_migrations(tmp_path) == []


def test_discover_migrations_missing_dir_raises(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        discover_migrations(tmp_path / "missing")
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/unit/test_migrate_runner.py -v`
Expected: FAIL — `Migration` and `discover_migrations` not defined.

- [ ] **Step 3: Implement**

```python
# Append to polyagent/scripts/migrate.py
import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Migration:
    version: str       # "001", "006", etc.
    filename: str      # "001_initial_schema.sql"
    sql: str
    checksum: str      # sha256 of sql bytes, hex


def discover_migrations(directory: Path) -> list[Migration]:
    """Return all *.sql files under `directory`, lex-sorted by filename.

    Version is the filename prefix before the first underscore.

    Raises:
        FileNotFoundError: if directory does not exist.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"migrations directory not found: {directory}")
    out: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        sql = path.read_text()
        version = path.name.split("_", 1)[0]
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        out.append(Migration(version=version, filename=path.name, sql=sql, checksum=checksum))
    return out
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_migrate_runner.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add polyagent/scripts/migrate.py tests/unit/test_migrate_runner.py
git commit -m "feat(migrate): discover migration files with checksums"
```

### Task 1.4: Read applied versions

**Files:**
- Modify: `polyagent/scripts/migrate.py`
- Modify: `tests/integration/test_migrate_runner.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/integration/test_migrate_runner.py
from polyagent.scripts.migrate import (
    AppliedRecord,
    ensure_schema_migrations_table,
    get_applied,
)


@pytest.mark.integration
def test_get_applied_returns_empty_on_fresh_db(empty_db_url):
    with psycopg.connect(empty_db_url) as conn:
        ensure_schema_migrations_table(conn)
        assert get_applied(conn) == {}


@pytest.mark.integration
def test_get_applied_reflects_inserted_rows(empty_db_url):
    with psycopg.connect(empty_db_url) as conn:
        ensure_schema_migrations_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schema_migrations (version, filename, checksum) "
                "VALUES (%s, %s, %s)",
                ("001", "001_first.sql", "abc"),
            )
        conn.commit()
        applied = get_applied(conn)
    assert "001" in applied
    assert applied["001"].filename == "001_first.sql"
    assert applied["001"].checksum == "abc"
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/integration/test_migrate_runner.py -v -m integration`
Expected: FAIL — `AppliedRecord`, `get_applied` not defined.

- [ ] **Step 3: Implement**

```python
# Append to polyagent/scripts/migrate.py
from datetime import datetime


@dataclass(frozen=True)
class AppliedRecord:
    version: str
    filename: str
    checksum: str
    applied_at: datetime


def get_applied(conn: psycopg.Connection) -> dict[str, AppliedRecord]:
    """Return {version: AppliedRecord} for all rows in schema_migrations."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version, filename, checksum, applied_at FROM schema_migrations"
        )
        rows = cur.fetchall()
    return {
        r[0]: AppliedRecord(version=r[0], filename=r[1], checksum=r[2], applied_at=r[3])
        for r in rows
    }
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/integration/test_migrate_runner.py -v -m integration`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add polyagent/scripts/migrate.py tests/integration/test_migrate_runner.py
git commit -m "feat(migrate): get_applied reads schema_migrations"
```

### Task 1.5: Apply a single migration in a transaction

**Files:**
- Modify: `polyagent/scripts/migrate.py`
- Modify: `tests/integration/test_migrate_runner.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/integration/test_migrate_runner.py
from polyagent.scripts.migrate import apply_migration


@pytest.mark.integration
def test_apply_migration_runs_sql_and_records_row(empty_db_url):
    m = Migration(
        version="100",
        filename="100_make_table.sql",
        sql="CREATE TABLE widget (id INT PRIMARY KEY);",
        checksum="deadbeef",
    )
    with psycopg.connect(empty_db_url) as conn:
        ensure_schema_migrations_table(conn)
        apply_migration(conn, m)
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'widget'")
            assert cur.fetchone() is not None
        applied = get_applied(conn)
    assert "100" in applied
    assert applied["100"].checksum == "deadbeef"


@pytest.mark.integration
def test_apply_migration_rolls_back_on_sql_error(empty_db_url):
    m = Migration(
        version="200",
        filename="200_bad.sql",
        sql="CREATE TABLE good (id INT); SELECT * FROM nonexistent;",
        checksum="abc",
    )
    with psycopg.connect(empty_db_url) as conn:
        ensure_schema_migrations_table(conn)
        with pytest.raises(psycopg.errors.UndefinedTable):
            apply_migration(conn, m)
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'good'")
            assert cur.fetchone() is None
        assert get_applied(conn) == {}
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/integration/test_migrate_runner.py -v -m integration`
Expected: FAIL — `apply_migration` not defined.

- [ ] **Step 3: Implement**

```python
# Append to polyagent/scripts/migrate.py
def apply_migration(conn: psycopg.Connection, m: Migration) -> None:
    """Execute the migration in a transaction, then record it.

    On any error the transaction is rolled back (by conn.transaction())
    and the exception propagates.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(m.sql)
            cur.execute(
                "INSERT INTO schema_migrations (version, filename, checksum) "
                "VALUES (%s, %s, %s)",
                (m.version, m.filename, m.checksum),
            )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/integration/test_migrate_runner.py -v -m integration`
Expected: PASS for both new tests.

- [ ] **Step 5: Commit**

```bash
git add polyagent/scripts/migrate.py tests/integration/test_migrate_runner.py
git commit -m "feat(migrate): apply_migration runs SQL in a transaction"
```

### Task 1.6: Drift detection

**Files:**
- Modify: `polyagent/scripts/migrate.py`
- Modify: `tests/unit/test_migrate_runner.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/unit/test_migrate_runner.py
from datetime import datetime

from polyagent.scripts.migrate import DriftError, Migration, AppliedRecord, plan_actions


def _ar(version: str, checksum: str) -> AppliedRecord:
    return AppliedRecord(
        version=version, filename=f"{version}.sql", checksum=checksum,
        applied_at=datetime(2026, 1, 1),
    )


def _mig(version: str, checksum: str) -> Migration:
    return Migration(version=version, filename=f"{version}.sql", sql="SELECT 1;", checksum=checksum)


def test_plan_actions_skips_matching_checksums():
    found = [_mig("001", "a"), _mig("002", "b")]
    applied = {"001": _ar("001", "a")}
    pending = plan_actions(found, applied)
    assert [m.version for m in pending] == ["002"]


def test_plan_actions_raises_on_drift():
    found = [_mig("001", "a")]
    applied = {"001": _ar("001", "DIFFERENT")}
    with pytest.raises(DriftError) as ei:
        plan_actions(found, applied)
    assert "001" in str(ei.value)


def test_plan_actions_empty_when_all_applied():
    found = [_mig("001", "a"), _mig("002", "b")]
    applied = {"001": _ar("001", "a"), "002": _ar("002", "b")}
    assert plan_actions(found, applied) == []
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/unit/test_migrate_runner.py -v`
Expected: FAIL — `DriftError`, `plan_actions` not defined.

- [ ] **Step 3: Implement**

```python
# Append to polyagent/scripts/migrate.py
class DriftError(RuntimeError):
    """Raised when an applied migration's checksum does not match its file."""


def plan_actions(
    found: list[Migration], applied: dict[str, AppliedRecord]
) -> list[Migration]:
    """Return migrations needing application, in order. Raise DriftError on mismatch."""
    pending: list[Migration] = []
    for m in found:
        rec = applied.get(m.version)
        if rec is None:
            pending.append(m)
            continue
        if rec.checksum != m.checksum:
            raise DriftError(
                f"checksum drift on version {m.version} ({m.filename}): "
                f"applied={rec.checksum[:12]}... file={m.checksum[:12]}..."
            )
    return pending
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_migrate_runner.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add polyagent/scripts/migrate.py tests/unit/test_migrate_runner.py
git commit -m "feat(migrate): drift detection via plan_actions"
```

### Task 1.7: `up` end-to-end orchestration

**Files:**
- Modify: `polyagent/scripts/migrate.py`
- Modify: `tests/integration/test_migrate_runner.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/integration/test_migrate_runner.py
from polyagent.scripts.migrate import migrate_up


@pytest.mark.integration
def test_migrate_up_applies_all_pending(empty_db_url, tmp_path):
    (tmp_path / "001_a.sql").write_text("CREATE TABLE a (id INT);")
    (tmp_path / "002_b.sql").write_text("CREATE TABLE b (id INT);")
    with psycopg.connect(empty_db_url) as conn:
        applied = migrate_up(conn, tmp_path)
    assert [m.version for m in applied] == ["001", "002"]
    with psycopg.connect(empty_db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name IN ('a', 'b') ORDER BY table_name"
        )
        names = [r[0] for r in cur.fetchall()]
    assert names == ["a", "b"]


@pytest.mark.integration
def test_migrate_up_is_noop_on_second_run(empty_db_url, tmp_path):
    (tmp_path / "001_a.sql").write_text("CREATE TABLE a (id INT);")
    with psycopg.connect(empty_db_url) as conn:
        first = migrate_up(conn, tmp_path)
        second = migrate_up(conn, tmp_path)
    assert [m.version for m in first] == ["001"]
    assert second == []


@pytest.mark.integration
def test_migrate_up_raises_drift_after_file_edit(empty_db_url, tmp_path):
    (tmp_path / "001_a.sql").write_text("CREATE TABLE a (id INT);")
    with psycopg.connect(empty_db_url) as conn:
        migrate_up(conn, tmp_path)
        (tmp_path / "001_a.sql").write_text("CREATE TABLE a (id INT, extra TEXT);")
        with pytest.raises(DriftError):
            migrate_up(conn, tmp_path)
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/integration/test_migrate_runner.py -v -m integration`
Expected: FAIL — `migrate_up` not defined.

- [ ] **Step 3: Implement**

```python
# Append to polyagent/scripts/migrate.py
def migrate_up(conn: psycopg.Connection, migrations_dir: Path) -> list[Migration]:
    """Apply all pending migrations. Return the list of migrations applied."""
    ensure_schema_migrations_table(conn)
    found = discover_migrations(migrations_dir)
    applied = get_applied(conn)
    pending = plan_actions(found, applied)
    for m in pending:
        apply_migration(conn, m)
    return pending
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/integration/test_migrate_runner.py -v -m integration`
Expected: PASS for all three tests.

- [ ] **Step 5: Commit**

```bash
git add polyagent/scripts/migrate.py tests/integration/test_migrate_runner.py
git commit -m "feat(migrate): migrate_up applies pending migrations"
```

### Task 1.8: `status` and `baseline`

**Files:**
- Modify: `polyagent/scripts/migrate.py`
- Modify: `tests/integration/test_migrate_runner.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/integration/test_migrate_runner.py
from polyagent.scripts.migrate import migrate_baseline, migrate_status


@pytest.mark.integration
def test_migrate_baseline_records_files_without_executing(empty_db_url, tmp_path):
    (tmp_path / "001_a.sql").write_text("RAISE syntax_error_intentional;")
    with psycopg.connect(empty_db_url) as conn:
        recorded = migrate_baseline(conn, tmp_path)
        assert [m.version for m in recorded] == ["001"]
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'a'")
            assert cur.fetchone() is None
        applied = get_applied(conn)
    assert "001" in applied


@pytest.mark.integration
def test_migrate_status_lists_applied_pending_drifted(empty_db_url, tmp_path):
    (tmp_path / "001_a.sql").write_text("CREATE TABLE a (id INT);")
    (tmp_path / "002_b.sql").write_text("CREATE TABLE b (id INT);")
    with psycopg.connect(empty_db_url) as conn:
        migrate_up(conn, tmp_path)  # applies both
        (tmp_path / "002_b.sql").write_text("CREATE TABLE b (id INT, x TEXT);")  # drift
        (tmp_path / "003_c.sql").write_text("CREATE TABLE c (id INT);")  # pending
        report = migrate_status(conn, tmp_path)
    assert [m.version for m in report.applied] == ["001"]
    assert [m.version for m in report.pending] == ["003"]
    assert [m.version for m in report.drifted] == ["002"]
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/integration/test_migrate_runner.py -v -m integration`
Expected: FAIL — `migrate_baseline`, `migrate_status` not defined.

- [ ] **Step 3: Implement**

```python
# Append to polyagent/scripts/migrate.py
@dataclass(frozen=True)
class StatusReport:
    applied: list[Migration]   # checksum matches
    pending: list[Migration]   # not in schema_migrations
    drifted: list[Migration]   # in schema_migrations but checksum differs


def migrate_baseline(conn: psycopg.Connection, migrations_dir: Path) -> list[Migration]:
    """Record all migration files as applied without executing them.

    Skips any version already in schema_migrations.
    """
    ensure_schema_migrations_table(conn)
    found = discover_migrations(migrations_dir)
    applied = get_applied(conn)
    recorded: list[Migration] = []
    with conn.cursor() as cur:
        for m in found:
            if m.version in applied:
                continue
            cur.execute(
                "INSERT INTO schema_migrations (version, filename, checksum) "
                "VALUES (%s, %s, %s)",
                (m.version, m.filename, m.checksum),
            )
            recorded.append(m)
    conn.commit()
    return recorded


def migrate_status(conn: psycopg.Connection, migrations_dir: Path) -> StatusReport:
    """Categorise migrations as applied / pending / drifted."""
    ensure_schema_migrations_table(conn)
    found = discover_migrations(migrations_dir)
    applied_map = get_applied(conn)
    applied: list[Migration] = []
    pending: list[Migration] = []
    drifted: list[Migration] = []
    for m in found:
        rec = applied_map.get(m.version)
        if rec is None:
            pending.append(m)
        elif rec.checksum == m.checksum:
            applied.append(m)
        else:
            drifted.append(m)
    return StatusReport(applied=applied, pending=pending, drifted=drifted)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/integration/test_migrate_runner.py -v -m integration`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add polyagent/scripts/migrate.py tests/integration/test_migrate_runner.py
git commit -m "feat(migrate): status and baseline subroutines"
```

### Task 1.9: Click CLI subcommand

**Files:**
- Create: `polyagent/cli/migrate_cmd.py`
- Modify: `polyagent/cli/main.py`

- [ ] **Step 1: Implement (no separate test — CLI is a thin shell over tested functions)**

```python
# polyagent/cli/migrate_cmd.py
"""`polyagent migrate` subcommand: up / status / baseline."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import click
import psycopg

from polyagent.scripts.migrate import (
    DriftError,
    migrate_baseline,
    migrate_status,
    migrate_up,
)

_DEFAULT_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"


def _connect() -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        click.echo("DATABASE_URL not set", err=True)
        sys.exit(2)
    return psycopg.connect(url)


@click.group()
def migrate() -> None:
    """Database migration runner."""


@migrate.command("up")
@click.option("--dir", "directory", type=click.Path(path_type=Path),
              default=_DEFAULT_DIR, show_default=True,
              help="Migrations directory.")
def up_cmd(directory: Path) -> None:
    """Apply pending migrations."""
    try:
        with _connect() as conn:
            applied = migrate_up(conn, directory)
    except DriftError as exc:
        click.echo(f"DRIFT: {exc}", err=True)
        sys.exit(1)
    if not applied:
        click.echo("Nothing to apply.")
        return
    for m in applied:
        click.echo(f"applied {m.version} {m.filename}")


@migrate.command("status")
@click.option("--dir", "directory", type=click.Path(path_type=Path),
              default=_DEFAULT_DIR, show_default=True)
def status_cmd(directory: Path) -> None:
    """Show applied / pending / drifted migrations."""
    with _connect() as conn:
        report = migrate_status(conn, directory)
    click.echo("Applied:")
    for m in report.applied:
        click.echo(f"  {m.version} {m.filename}")
    click.echo("Pending:")
    for m in report.pending:
        click.echo(f"  {m.version} {m.filename}")
    click.echo("Drifted:")
    for m in report.drifted:
        click.echo(f"  {m.version} {m.filename}")
    if report.drifted:
        sys.exit(1)


@migrate.command("baseline")
@click.option("--dir", "directory", type=click.Path(path_type=Path),
              default=_DEFAULT_DIR, show_default=True)
def baseline_cmd(directory: Path) -> None:
    """Record all migration files as applied without executing them."""
    with _connect() as conn:
        recorded = migrate_baseline(conn, directory)
    if not recorded:
        click.echo("Nothing to baseline.")
        return
    for m in recorded:
        click.echo(f"baselined {m.version} {m.filename}")
```

```python
# polyagent/cli/main.py — add to existing file
from polyagent.cli.migrate_cmd import migrate

# ... existing imports and code ...

cli.add_command(migrate)
```

- [ ] **Step 2: Smoke-test the CLI**

Run: `polyagent migrate --help`
Expected: shows `up`, `status`, `baseline` subcommands.

Run: `polyagent migrate status` (against local dev DB)
Expected: lists migrations 001-005 as Pending (since `schema_migrations` does not exist yet, they all show as pending — operator runs `baseline` next).

- [ ] **Step 3: Commit**

```bash
git add polyagent/cli/migrate_cmd.py polyagent/cli/main.py
git commit -m "feat(cli): polyagent migrate {up,status,baseline}"
```

### Task 1.10: Operations doc + push

**Files:**
- Create: `docs/refactor/quant-multi-asset-ops.md`

- [ ] **Step 1: Write operations notes**

```markdown
# Quant Refactor — Operational Notes

## After PR 1 ships

On every host that has a PolyAgent database (dev, prod):

```bash
polyagent migrate baseline   # records 001-005 as applied without re-executing
polyagent migrate status     # verify all five appear under "Applied:"
```

Then wire `polyagent migrate up` into bot startup:

- compose: add to `command:` of the bot service: `sh -c 'polyagent migrate up && polyagent run'`
- systemd: `ExecStartPre=/path/to/polyagent migrate up`

## After PR 5 ships (migration 006)

Bot startup will auto-apply migration 006 via the `migrate up` step. Verify
with `polyagent migrate status` post-deploy — `006` should be under Applied.

## After PR 6 ships

Update `.env` on each host:

```
# Remove
BTC5M_ENABLED=true
BTC5M_VOL_WINDOW_S=300
BTC5M_EDGE_THRESHOLD=0.05
BTC5M_POSITION_SIZE_USD=5.0
BTC5M_FEES_BPS=0.0
BTC5M_SPOT_POLL_S=2.0
BTC5M_MARKET_POLL_S=60
CRYPTO_QUANT_ENABLED=true
CRYPTO_QUANT_BTC_VOL=0.60
CRYPTO_QUANT_ETH_VOL=0.75

# Add
QUANT_SHORT_ENABLED=true
QUANT_MARKET_POLL_S=60
QUANT_POSITION_SIZE_USD=5.0
# Per-asset overrides (optional — registry defaults shown):
# QUANT_BTC_VOL=0.60
# QUANT_BTC_EDGE_THRESHOLD=0.05
# QUANT_ETH_VOL=0.75
# QUANT_ETH_EDGE_THRESHOLD=0.05
```
```

- [ ] **Step 2: Commit and push the branch**

```bash
git add docs/refactor/quant-multi-asset-ops.md
git commit -m "docs(refactor): operational notes for quant rollout"
git push -u origin refactor/quant-pr1-migrate-runner
```

- [ ] **Step 3: Open PR**

Title: `refactor(quant) PR 1/6: migration runner + schema_migrations`
Body links to spec + plan and lists deploy steps.

---

# PR 2 — Move estimator and extract `compute_vol`

**Goal:** Move pure-math modules from `polyagent/services/btc5m/` to `polyagent/services/quant/core/`. Extract `compute_vol(spec, source, horizon_s)` so per-tenor calibration lives in one place. Old import paths re-exported.

**Branch:** `refactor/quant-pr2-core-extraction` (branched from main after PR 1 lands).

### Task 2.1: Create `quant/core/` skeleton

**Files:**
- Create: `polyagent/services/quant/__init__.py` (empty)
- Create: `polyagent/services/quant/core/__init__.py`

- [ ] **Step 1: Create empty package markers**

```python
# polyagent/services/quant/__init__.py
"""Multi-asset quant subsystem (short-horizon + strike markets)."""
```

```python
# polyagent/services/quant/core/__init__.py
"""Pure-math primitives: estimator, vol calibration, P&L."""
```

- [ ] **Step 2: Verify import works**

Run: `python -c "import polyagent.services.quant.core"`
Expected: no error.

- [ ] **Step 3: Commit**

```bash
git add polyagent/services/quant/__init__.py polyagent/services/quant/core/__init__.py
git commit -m "feat(quant): skeleton package + core/"
```

### Task 2.2: Move `estimator.py`

**Files:**
- Create: `polyagent/services/quant/core/estimator.py`
- Modify: `polyagent/services/btc5m/estimator.py` (replace body with re-export)
- Tests stay where they are (existing `tests/unit/test_btc5m_estimator.py` if present, or whatever name); they'll continue to pass via the re-export.

- [ ] **Step 1: Verify existing tests pass**

Run: `pytest tests/ -v -k estimator`
Expected: PASS for all current estimator tests.

- [ ] **Step 2: Move file**

```bash
git mv polyagent/services/btc5m/estimator.py polyagent/services/quant/core/estimator.py
```

- [ ] **Step 3: Update module docstring (top of new file)**

```python
"""Lognormal P(up) estimator for binary horizon markets.

Pure function. No I/O. Never raises. Parameterized on time-to-maturity so
the same code serves 5m bets and 30d strikes across BTC, ETH, FX, etc.
"""
```

- [ ] **Step 4: Add re-export at old path**

```python
# polyagent/services/btc5m/estimator.py — recreate as re-export
"""Re-export shim. Real implementation lives in polyagent.services.quant.core.estimator."""
from polyagent.services.quant.core.estimator import estimate_up_probability

__all__ = ["estimate_up_probability"]
```

- [ ] **Step 5: Run all tests**

Run: `pytest tests/ -v`
Expected: PASS — both `from polyagent.services.btc5m.estimator import ...` and `from polyagent.services.quant.core.estimator import ...` resolve to the same callable.

- [ ] **Step 6: Commit**

```bash
git add polyagent/services/quant/core/estimator.py polyagent/services/btc5m/estimator.py
git commit -m "refactor(quant): move estimator to quant.core; re-export shim"
```

### Task 2.3: Move `compute_pnl` to `quant/core/pnl.py`

**Files:**
- Create: `polyagent/services/quant/core/pnl.py`
- Modify: `polyagent/services/btc5m/resolver.py`

- [ ] **Step 1: Verify existing pnl tests pass**

Run: `pytest tests/ -v -k pnl`
Expected: PASS.

- [ ] **Step 2: Create `quant/core/pnl.py`**

```python
# polyagent/services/quant/core/pnl.py
"""Realized P&L for binary paper trades.

`size` is USD notional. YES side profits `(1 - fill_price)` per unit
notional if outcome is YES, loses `fill_price` if NO. NO side mirrors.
"""
from __future__ import annotations

from decimal import Decimal


def compute_pnl(
    side: str,
    fill_price: Decimal,
    outcome: str,
    size: Decimal,
) -> Decimal:
    """Signed P&L in USD for a binary paper trade."""
    if side == "YES":
        return size * (Decimal("1") - fill_price) if outcome == "YES" else -size * fill_price
    return size * (Decimal("1") - fill_price) if outcome == "NO" else -size * fill_price
```

- [ ] **Step 3: Update `btc5m/resolver.py` to import from new location**

In `polyagent/services/btc5m/resolver.py`, replace the local `compute_pnl` definition with:

```python
from polyagent.services.quant.core.pnl import compute_pnl
```

(Delete the original function body in `resolver.py`.)

- [ ] **Step 4: Run tests**

Run: `pytest tests/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add polyagent/services/quant/core/pnl.py polyagent/services/btc5m/resolver.py
git commit -m "refactor(quant): move compute_pnl to quant.core.pnl"
```

### Task 2.4: Vol calibration types

**Files:**
- Create: `polyagent/services/quant/core/vol.py`
- Create: `tests/unit/test_quant_core_vol.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_quant_core_vol.py
from dataclasses import dataclass
from decimal import Decimal

import pytest

from polyagent.services.quant.core.vol import (
    VolCalibration, VolMethod, compute_vol,
)


class _FakeSource:
    def __init__(self, rolling_value: float):
        self._v = rolling_value
        self.last_window_s: int | None = None
    def realized_vol(self, window_s: int) -> float:
        self.last_window_s = window_s
        return self._v


@dataclass
class _Spec:
    default_vol: float
    vol_calibration: VolCalibration


def test_compute_vol_fixed_returns_fixed_value():
    spec = _Spec(default_vol=0.6, vol_calibration=VolCalibration(method=VolMethod.FIXED, fixed_value=0.42))
    assert compute_vol(spec, _FakeSource(0.0), horizon_s=300.0) == 0.42


def test_compute_vol_fixed_falls_back_to_default_when_unset():
    spec = _Spec(default_vol=0.6, vol_calibration=VolCalibration(method=VolMethod.FIXED))
    assert compute_vol(spec, _FakeSource(0.0), horizon_s=300.0) == 0.6


def test_compute_vol_rolling_uses_clamped_lookback():
    spec = _Spec(default_vol=0.6, vol_calibration=VolCalibration(
        method=VolMethod.ROLLING_REALIZED,
        rolling_min_s=300, rolling_max_s=86400, rolling_horizon_multiplier=4.0,
    ))
    src = _FakeSource(0.5)
    # 60 * 4 = 240 < min 300 → clamped up to 300
    assert compute_vol(spec, src, horizon_s=60.0) == 0.5
    assert src.last_window_s == 300
    # 1h * 4 = 4h = 14400 → in range
    src2 = _FakeSource(0.5)
    compute_vol(spec, src2, horizon_s=3600.0)
    assert src2.last_window_s == 14400
    # 30d * 4 = much more than 86400 → clamped down to 86400
    src3 = _FakeSource(0.5)
    compute_vol(spec, src3, horizon_s=30 * 86400.0)
    assert src3.last_window_s == 86400


def test_compute_vol_rolling_falls_back_to_default_when_zero():
    spec = _Spec(default_vol=0.6, vol_calibration=VolCalibration(
        method=VolMethod.ROLLING_REALIZED,
    ))
    assert compute_vol(spec, _FakeSource(0.0), horizon_s=300.0) == 0.6


def test_compute_vol_hybrid_short_horizon_uses_rolling():
    spec = _Spec(default_vol=0.6, vol_calibration=VolCalibration(
        method=VolMethod.HYBRID, fixed_value=0.99, hybrid_threshold_s=14400,
    ))
    assert compute_vol(spec, _FakeSource(0.4), horizon_s=300.0) == 0.4


def test_compute_vol_hybrid_long_horizon_uses_fixed():
    spec = _Spec(default_vol=0.6, vol_calibration=VolCalibration(
        method=VolMethod.HYBRID, fixed_value=0.99, hybrid_threshold_s=14400,
    ))
    assert compute_vol(spec, _FakeSource(0.4), horizon_s=86400.0) == 0.99
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/unit/test_quant_core_vol.py -v`
Expected: FAIL — `polyagent.services.quant.core.vol` module not present.

- [ ] **Step 3: Implement**

```python
# polyagent/services/quant/core/vol.py
"""Per-tenor vol calibration policy.

`compute_vol(spec, source, horizon_s)` is the single call site. Decider
and strike service both use it. Three methods:

- ROLLING_REALIZED: lookback = clamp(min, k * horizon, max); fall back
  to default_vol if rolling returns 0 (insufficient samples).
- FIXED: return fixed_value or default_vol.
- HYBRID: ROLLING_REALIZED for short horizons, FIXED past
  hybrid_threshold_s.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class VolMethod(str, Enum):
    ROLLING_REALIZED = "ROLLING_REALIZED"
    FIXED = "FIXED"
    HYBRID = "HYBRID"


@dataclass(frozen=True)
class VolCalibration:
    method: VolMethod
    rolling_min_s: int = 300
    rolling_max_s: int = 24 * 3600
    rolling_horizon_multiplier: float = 4.0
    fixed_value: float | None = None
    hybrid_threshold_s: int = 4 * 3600


class _SupportsRollingVol(Protocol):
    def realized_vol(self, window_s: int) -> float: ...


class _SpecLike(Protocol):
    @property
    def default_vol(self) -> float: ...
    @property
    def vol_calibration(self) -> VolCalibration: ...


def _rolling(spec: _SpecLike, source: _SupportsRollingVol, horizon_s: float) -> float:
    cal = spec.vol_calibration
    raw = horizon_s * cal.rolling_horizon_multiplier
    lookback = max(cal.rolling_min_s, min(int(raw), cal.rolling_max_s))
    v = source.realized_vol(lookback)
    return v if v > 0 else spec.default_vol


def _fixed(spec: _SpecLike) -> float:
    return spec.vol_calibration.fixed_value if spec.vol_calibration.fixed_value is not None else spec.default_vol


def compute_vol(spec: _SpecLike, source: _SupportsRollingVol, horizon_s: float) -> float:
    """Return annualised σ for `spec` over `horizon_s`."""
    cal = spec.vol_calibration
    if cal.method == VolMethod.FIXED:
        return _fixed(spec)
    if cal.method == VolMethod.ROLLING_REALIZED:
        return _rolling(spec, source, horizon_s)
    if cal.method == VolMethod.HYBRID:
        if horizon_s < cal.hybrid_threshold_s:
            return _rolling(spec, source, horizon_s)
        return _fixed(spec)
    raise ValueError(f"unknown VolMethod: {cal.method!r}")
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_quant_core_vol.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add polyagent/services/quant/core/vol.py tests/unit/test_quant_core_vol.py
git commit -m "feat(quant): vol calibration policy (rolling/fixed/hybrid)"
```

### Task 2.5: Wire `compute_vol` into the existing btc5m decider

**Files:**
- Modify: `polyagent/services/btc5m/decider.py`

- [ ] **Step 1: Construct a temporary spec for BTC at the call site**

In `polyagent/services/btc5m/decider.py`, change the vol call from `vol = self._spot.realized_vol(window_s=self._vol_window_s)` to use `compute_vol`:

```python
# Add at top of file
from polyagent.services.quant.core.vol import (
    VolCalibration, VolMethod, compute_vol,
)

# Inside Btc5mDecider, replace `vol = self._spot.realized_vol(...)` line with:
vol_spec = type("_Inline", (), {
    "default_vol": 0.60,
    "vol_calibration": VolCalibration(
        method=VolMethod.ROLLING_REALIZED,
        rolling_min_s=self._vol_window_s,
        rolling_max_s=self._vol_window_s,
        rolling_horizon_multiplier=1.0,  # preserves existing behavior exactly
    ),
})
vol = compute_vol(vol_spec, self._spot, horizon_s=ttm)
```

This is a behavior-preserving change: with `rolling_min_s == rolling_max_s == self._vol_window_s` and `multiplier = 1.0`, the lookback is always exactly `self._vol_window_s` — identical to today.

- [ ] **Step 2: Run all existing decider tests**

Run: `pytest tests/ -v -k decider`
Expected: PASS — behavior is bit-for-bit identical.

- [ ] **Step 3: Commit**

```bash
git add polyagent/services/btc5m/decider.py
git commit -m "refactor(btc5m): route decider vol through compute_vol shim"
```

### Task 2.6: Push and PR

- [ ] **Step 1: Push branch**

```bash
git push -u origin refactor/quant-pr2-core-extraction
```

- [ ] **Step 2: Open PR**

Title: `refactor(quant) PR 2/6: move estimator + extract compute_vol`
Body notes that no behavior changes; old import paths preserved.

---

# PR 3 — `AssetSpec` registry + Coinbase source

**Goal:** Add the typed asset registry and the new source abstractions. Nothing consumes the registry yet — pure addition.

**Branch:** `refactor/quant-pr3-registry`

### Task 3.1: Source Protocols

**Files:**
- Create: `polyagent/services/quant/assets/__init__.py`
- Create: `polyagent/services/quant/assets/sources/__init__.py`
- Create: `polyagent/services/quant/assets/sources/base.py`

- [ ] **Step 1: Create files**

```python
# polyagent/services/quant/assets/__init__.py
"""Asset registry: AssetSpec, sources, env overrides."""
```

```python
# polyagent/services/quant/assets/sources/__init__.py
"""Concrete price/settlement source implementations."""
```

```python
# polyagent/services/quant/assets/sources/base.py
"""PriceSource and SettlementSource Protocols.

PriceSource is hot-path tick-frequency. SettlementSource is occasional
historical lookup. They are separate so an asset can have one without the
other (e.g. paper-only with no live ticks).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol


class PriceSource(Protocol):
    def tick(self) -> Decimal | None: ...
    def current(self) -> Decimal | None: ...
    def realized_vol(self, window_s: int) -> float: ...
    def close(self) -> None: ...


class SettlementSource(Protocol):
    def price_at(self, ts: datetime) -> Decimal | None: ...
    def source_id(self) -> str: ...
```

- [ ] **Step 2: Verify imports**

Run: `python -c "from polyagent.services.quant.assets.sources.base import PriceSource, SettlementSource"`
Expected: no error.

- [ ] **Step 3: Commit**

```bash
git add polyagent/services/quant/assets/__init__.py polyagent/services/quant/assets/sources/__init__.py polyagent/services/quant/assets/sources/base.py
git commit -m "feat(quant): PriceSource and SettlementSource Protocols"
```

### Task 3.2: Coinbase source (move + extend)

**Files:**
- Create: `polyagent/services/quant/assets/sources/coinbase.py`
- Create: `tests/unit/test_quant_assets_sources_coinbase.py`
- Modify: `polyagent/services/btc5m/spot.py` (re-export shim)

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_quant_assets_sources_coinbase.py
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from polyagent.services.quant.assets.sources.coinbase import CoinbaseSpotSource


class _FakeHttp:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[str] = []
    def get(self, url, params=None):
        self.calls.append(url)
        if not self._responses:
            raise RuntimeError("exhausted")
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


class _Resp:
    def __init__(self, status_code: int, json_body):
        self.status_code = status_code
        self._body = json_body
    def json(self):
        return self._body
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


def test_source_id_format():
    s = CoinbaseSpotSource("BTC-USD", http_client=_FakeHttp([]))
    assert s.source_id() == "coinbase:BTC-USD"
    s2 = CoinbaseSpotSource("ETH-USD", http_client=_FakeHttp([]))
    assert s2.source_id() == "coinbase:ETH-USD"


def test_tick_appends_to_buffer_and_returns_mid():
    http = _FakeHttp([_Resp(200, {"bid": "100.00", "ask": "102.00"})])
    s = CoinbaseSpotSource("BTC-USD", http_client=http)
    p = s.tick()
    assert p == Decimal("101.00")
    assert s.current() == Decimal("101.00")


def test_tick_returns_none_on_error():
    http = _FakeHttp([RuntimeError("boom")])
    s = CoinbaseSpotSource("BTC-USD", http_client=http)
    assert s.tick() is None
    assert s.current() is None


def test_price_at_uses_candle_endpoint():
    target = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    candle = [int(target.timestamp()), 100.0, 105.0, 99.0, 103.0, 1.0]
    http = _FakeHttp([_Resp(200, [candle])])
    s = CoinbaseSpotSource("BTC-USD", http_client=http)
    assert s.price_at(target) == Decimal("103.0")
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/unit/test_quant_assets_sources_coinbase.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement (move + adapt from `btc5m/spot.py`)**

```python
# polyagent/services/quant/assets/sources/coinbase.py
"""Coinbase price + settlement source.

Implements both PriceSource (tick / current / realized_vol) and
SettlementSource (price_at) against the public Coinbase Exchange API.
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from datetime import datetime
from decimal import Decimal

import httpx

logger = logging.getLogger("polyagent.services.quant.assets.sources.coinbase")

_TICKER_URL_FMT = "https://api.exchange.coinbase.com/products/{product}/ticker"
_CANDLES_URL_FMT = "https://api.exchange.coinbase.com/products/{product}/candles"
_SECONDS_PER_YEAR = 365.25 * 24 * 3600


class CoinbaseSpotSource:
    """In-memory rolling buffer of Coinbase mid prices, plus historical lookup."""

    def __init__(
        self,
        product: str = "BTC-USD",
        max_age_s: int = 3600,
        timeout_s: float = 5.0,
        http_client=None,
    ) -> None:
        self._product = product
        self._ticker_url = _TICKER_URL_FMT.format(product=product)
        self._candles_url = _CANDLES_URL_FMT.format(product=product)
        self._max_age_s = max_age_s
        self._buf: deque[tuple[float, Decimal]] = deque()
        self._http = http_client or httpx.Client(timeout=timeout_s)

    @property
    def product(self) -> str:
        return self._product

    def source_id(self) -> str:
        return f"coinbase:{self._product}"

    def tick(self) -> Decimal | None:
        try:
            resp = self._http.get(self._ticker_url)
            resp.raise_for_status()
            body = resp.json()
            bid = Decimal(str(body["bid"]))
            ask = Decimal(str(body["ask"]))
            price = (bid + ask) / Decimal("2")
        except Exception as exc:
            logger.warning("%s tick failed: %s", self._product, exc)
            return None
        now = time.time()
        self._buf.append((now, price))
        cutoff = now - self._max_age_s
        while self._buf and self._buf[0][0] < cutoff:
            self._buf.popleft()
        return price

    def current(self) -> Decimal | None:
        if not self._buf:
            return None
        return self._buf[-1][1]

    def realized_vol(self, window_s: int = 300) -> float:
        if not self._buf:
            return 0.0
        latest_ts = self._buf[-1][0]
        cutoff = latest_ts - window_s
        samples = [(t, p) for (t, p) in self._buf if t >= cutoff]
        if len(samples) < 2:
            return 0.0
        log_returns: list[float] = []
        for i in range(1, len(samples)):
            prev_p = float(samples[i - 1][1])
            curr_p = float(samples[i][1])
            if prev_p <= 0 or curr_p <= 0:
                continue
            log_returns.append(math.log(curr_p / prev_p))
        if len(log_returns) < 2:
            return 0.0
        mean = sum(log_returns) / len(log_returns)
        variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
        span_s = samples[-1][0] - samples[0][0]
        if span_s <= 0:
            return 0.0
        per_s_variance = variance * len(log_returns) / span_s
        return math.sqrt(per_s_variance * _SECONDS_PER_YEAR)

    def price_at(self, ts: datetime) -> Decimal | None:
        target = int(ts.timestamp())
        try:
            resp = self._http.get(
                self._candles_url,
                params={"granularity": 60, "start": target - 60, "end": target + 60},
            )
            resp.raise_for_status()
            candles = resp.json()
        except Exception as exc:
            logger.warning("%s candle fetch failed for ts=%s: %s", self._product, ts, exc)
            return None
        if not candles:
            return None
        # candles are [time, low, high, open, close, volume]
        best = min(candles, key=lambda c: abs(c[0] - target))
        return Decimal(str(best[4]))

    def close(self) -> None:
        self._http.close()
```

- [ ] **Step 4: Update `btc5m/spot.py` to re-export**

```python
# polyagent/services/btc5m/spot.py — replace entire file
"""Re-export shim. Real implementation lives in
polyagent.services.quant.assets.sources.coinbase.
"""
from polyagent.services.quant.assets.sources.coinbase import CoinbaseSpotSource

# Back-compat aliases used by the old btc5m subsystem.
BtcSpotSource = CoinbaseSpotSource

__all__ = ["CoinbaseSpotSource", "BtcSpotSource"]
```

- [ ] **Step 5: Run all tests**

Run: `pytest tests/ -v`
Expected: PASS — both old and new import paths resolve to the same class. Existing `btc5m` tests (including `test_btc5m_repo`, the worker integration tests, etc.) continue to pass.

- [ ] **Step 6: Commit**

```bash
git add polyagent/services/quant/assets/sources/coinbase.py polyagent/services/btc5m/spot.py tests/unit/test_quant_assets_sources_coinbase.py
git commit -m "feat(quant): CoinbaseSpotSource (PriceSource + SettlementSource)"
```

### Task 3.3: AssetSpec types

**Files:**
- Create: `polyagent/services/quant/assets/spec.py`

- [ ] **Step 1: Implement (no test — pure data class, exercised via registry tests in 3.4)**

```python
# polyagent/services/quant/assets/spec.py
"""Typed asset specification.

The registry in `registry.py` declares one AssetSpec per supported asset.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from polyagent.services.quant.assets.sources.base import PriceSource, SettlementSource
from polyagent.services.quant.core.vol import VolCalibration


class AssetClass(str, Enum):
    CRYPTO = "CRYPTO"
    FX = "FX"
    COMMODITY = "COMMODITY"


class MarketFamily(str, Enum):
    SHORT_HORIZON = "SHORT_HORIZON"
    STRIKE = "STRIKE"
    RANGE = "RANGE"


PriceSourceFactory = Callable[[], PriceSource]
SettlementSourceFactory = Callable[[], SettlementSource]


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
    slug_token: str = ""
    question_keywords: tuple[str, ...] = ()
```

- [ ] **Step 2: Verify import**

Run: `python -c "from polyagent.services.quant.assets.spec import AssetSpec, AssetClass, MarketFamily"`
Expected: no error.

- [ ] **Step 3: Commit**

```bash
git add polyagent/services/quant/assets/spec.py
git commit -m "feat(quant): AssetSpec, AssetClass, MarketFamily types"
```

### Task 3.4: Registry with read API + env overrides

**Files:**
- Create: `polyagent/services/quant/assets/registry.py`
- Create: `tests/unit/test_quant_assets_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_quant_assets_registry.py
import pytest

from polyagent.services.quant.assets.registry import (
    ASSETS, get, enabled_for, live_eligible, apply_env_overrides,
)
from polyagent.services.quant.assets.spec import (
    AssetClass, AssetSpec, MarketFamily,
)
from polyagent.services.quant.core.vol import VolCalibration, VolMethod


def test_btc_and_eth_registered():
    assert "BTC" in ASSETS
    assert "ETH" in ASSETS
    assert ASSETS["BTC"].asset_class == AssetClass.CRYPTO
    assert ASSETS["ETH"].asset_class == AssetClass.CRYPTO


def test_get_returns_spec_or_none():
    assert get("BTC").asset_id == "BTC"
    assert get("XAU") is None


def test_enabled_for_filters_by_market_family():
    short = enabled_for(MarketFamily.SHORT_HORIZON)
    strike = enabled_for(MarketFamily.STRIKE)
    short_ids = [s.asset_id for s in short]
    strike_ids = [s.asset_id for s in strike]
    assert "BTC" in short_ids
    assert "BTC" in strike_ids
    assert "ETH" in strike_ids


def test_live_eligible_excludes_paper_only(monkeypatch):
    base = ASSETS["BTC"]
    paper_btc = AssetSpec(
        asset_id="BTC",
        asset_class=AssetClass.CRYPTO,
        price_source=base.price_source,
        settlement_source=base.settlement_source,
        default_vol=0.6,
        vol_calibration=base.vol_calibration,
        supported_market_families=frozenset({MarketFamily.SHORT_HORIZON}),
        paper_only=True,
    )
    monkeypatch.setitem(ASSETS, "BTC", paper_btc)
    eligible = live_eligible(MarketFamily.SHORT_HORIZON)
    assert all(s.asset_id != "BTC" for s in eligible)


def test_apply_env_overrides_replaces_default_vol(monkeypatch):
    monkeypatch.setenv("QUANT_BTC_VOL", "0.85")
    spec = apply_env_overrides(ASSETS["BTC"])
    assert spec.default_vol == 0.85


def test_apply_env_overrides_replaces_edge_threshold(monkeypatch):
    monkeypatch.setenv("QUANT_BTC_EDGE_THRESHOLD", "0.10")
    spec = apply_env_overrides(ASSETS["BTC"])
    assert spec.edge_threshold == 0.10


def test_apply_env_overrides_no_env_returns_unchanged():
    original = ASSETS["BTC"]
    spec = apply_env_overrides(original)
    assert spec.default_vol == original.default_vol
    assert spec.edge_threshold == original.edge_threshold
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/unit/test_quant_assets_registry.py -v`
Expected: FAIL — `polyagent.services.quant.assets.registry` missing.

- [ ] **Step 3: Implement**

```python
# polyagent/services/quant/assets/registry.py
"""Asset registry: declares supported assets, read API, env overrides.

To add a new asset:
1. Append an entry to ASSETS below.
2. If the asset needs a new price/settlement source, add it under
   polyagent/services/quant/assets/sources/.
3. Update tests in tests/unit/test_quant_assets_registry.py.
"""
from __future__ import annotations

import os
from dataclasses import replace

from polyagent.services.quant.assets.sources.coinbase import CoinbaseSpotSource
from polyagent.services.quant.assets.spec import (
    AssetClass, AssetSpec, MarketFamily,
)
from polyagent.services.quant.core.vol import VolCalibration, VolMethod


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
        paper_only=False,
        fee_bps=0.0,
        edge_threshold=0.05,
        tick_interval_s=2.0,
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


def _bool_env(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _float_env(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def apply_env_overrides(spec: AssetSpec) -> AssetSpec:
    """Return a new AssetSpec with QUANT_<ASSET>_* env values applied."""
    a = spec.asset_id
    overrides: dict = {}
    if (v := _float_env(f"QUANT_{a}_VOL")) is not None:
        overrides["default_vol"] = v
        cal = spec.vol_calibration
        if cal.method in (VolMethod.FIXED, VolMethod.HYBRID):
            overrides["vol_calibration"] = replace(cal, fixed_value=v)
    if (v := _float_env(f"QUANT_{a}_EDGE_THRESHOLD")) is not None:
        overrides["edge_threshold"] = v
    if (v := _float_env(f"QUANT_{a}_FEE_BPS")) is not None:
        overrides["fee_bps"] = v
    if (b := _bool_env(f"QUANT_{a}_PAPER_ONLY")) is not None:
        overrides["paper_only"] = b
    if not overrides:
        return spec
    return replace(spec, **overrides)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_quant_assets_registry.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add polyagent/services/quant/assets/registry.py tests/unit/test_quant_assets_registry.py
git commit -m "feat(quant): asset registry with env override layer"
```

### Task 3.5: Push and PR

- [ ] **Step 1: Push**

```bash
git push -u origin refactor/quant-pr3-registry
```

- [ ] **Step 2: Open PR**

Title: `refactor(quant) PR 3/6: AssetSpec registry + Coinbase source`
Body notes: pure addition; nothing consumes the registry yet.

---

# PR 4 — Migrate `crypto_quant` → `quant/strike`

**Goal:** Replace `CryptoQuantService` with `QuantStrikeService` consuming the registry. Generalize the question parser. One-line brain.py import change. Delete `crypto_quant/`.

**Branch:** `refactor/quant-pr4-strike-migration`

### Task 4.1: Generalized question parser

**Files:**
- Create: `polyagent/services/quant/strike/__init__.py`
- Create: `polyagent/services/quant/strike/parser.py`
- Create: `tests/unit/test_quant_strike_parser.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_quant_strike_parser.py
from decimal import Decimal

from polyagent.services.quant.strike.parser import (
    ParsedStrike, StrikeKind, parse_question,
)


def test_parses_btc_above():
    p = parse_question("Will the price of Bitcoin be above $50,000 on Friday?")
    assert p == ParsedStrike(asset_id="BTC", kind=StrikeKind.UP, strike=Decimal("50000"))


def test_parses_eth_below():
    p = parse_question("Will the price of Ethereum be below $3,000 on Sunday?")
    assert p == ParsedStrike(asset_id="ETH", kind=StrikeKind.DOWN, strike=Decimal("3000"))


def test_parses_between_normalizes_low_high():
    p = parse_question("Will the price of BTC be between $80,000 and $70,000 on June 1?")
    assert p == ParsedStrike(
        asset_id="BTC", kind=StrikeKind.RANGE,
        strike=Decimal("70000"), upper_strike=Decimal("80000"),
    )


def test_unknown_asset_returns_none():
    assert parse_question("Will the price of Solana be above $200 on Friday?") is None


def test_unmatched_pattern_returns_none():
    assert parse_question("Random unrelated question?") is None


def test_empty_question_returns_none():
    assert parse_question("") is None
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/unit/test_quant_strike_parser.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# polyagent/services/quant/strike/__init__.py
"""Long-horizon strike-market handler. Brain integration seam."""
from polyagent.services.quant.strike.service import QuantStrikeService

__all__ = ["QuantStrikeService"]
```

```python
# polyagent/services/quant/strike/parser.py
"""Registry-driven question parser for strike markets.

Iterates registry.enabled_for(STRIKE), tries each asset's
question_keywords against the standard above/below/between patterns.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from polyagent.services.quant.assets.registry import enabled_for
from polyagent.services.quant.assets.spec import MarketFamily


class StrikeKind(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    RANGE = "RANGE"


@dataclass(frozen=True)
class ParsedStrike:
    asset_id: str
    kind: StrikeKind
    strike: Decimal
    upper_strike: Decimal | None = None


_NUM = r"\$([\d,]+(?:\.\d+)?)"


def _to_decimal(raw: str) -> Decimal:
    return Decimal(raw.replace(",", ""))


def _build_patterns_for_keyword(kw: str):
    """Return (RE_ABOVE, RE_BELOW, RE_BETWEEN) for a single keyword."""
    kw_re = re.escape(kw)
    above = re.compile(rf"\bWill the price of {kw_re} be above {_NUM}\b", re.IGNORECASE)
    below = re.compile(rf"\bWill the price of {kw_re} be below {_NUM}\b", re.IGNORECASE)
    between = re.compile(
        rf"\bWill the price of {kw_re} be between {_NUM} and {_NUM}\b", re.IGNORECASE,
    )
    return above, below, between


def parse_question(question: str) -> ParsedStrike | None:
    """Return a ParsedStrike for supported patterns, or None."""
    if not question:
        return None
    for spec in enabled_for(MarketFamily.STRIKE):
        for kw in spec.question_keywords:
            above_re, below_re, between_re = _build_patterns_for_keyword(kw)
            if (m := between_re.search(question)):
                low, high = _to_decimal(m.group(1)), _to_decimal(m.group(2))
                if low > high:
                    low, high = high, low
                return ParsedStrike(
                    asset_id=spec.asset_id, kind=StrikeKind.RANGE,
                    strike=low, upper_strike=high,
                )
            if (m := above_re.search(question)):
                return ParsedStrike(
                    asset_id=spec.asset_id, kind=StrikeKind.UP,
                    strike=_to_decimal(m.group(1)),
                )
            if (m := below_re.search(question)):
                return ParsedStrike(
                    asset_id=spec.asset_id, kind=StrikeKind.DOWN,
                    strike=_to_decimal(m.group(1)),
                )
    return None
```

> **Note:** the import `from polyagent.services.quant.strike.service import QuantStrikeService` in `__init__.py` will fail at this step until Task 4.2 lands. Either commit `__init__.py` empty for now and update in 4.2, or commit them together.

For simplicity, leave `__init__.py` empty in this task and add the export in Task 4.2.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_quant_strike_parser.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add polyagent/services/quant/strike/__init__.py polyagent/services/quant/strike/parser.py tests/unit/test_quant_strike_parser.py
git commit -m "feat(quant): registry-driven strike question parser"
```

### Task 4.2: `QuantStrikeService`

**Files:**
- Create: `polyagent/services/quant/strike/service.py`
- Create: `tests/unit/test_quant_strike_service.py`
- Modify: `polyagent/services/quant/strike/__init__.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_quant_strike_service.py
from decimal import Decimal

from polyagent.services.quant.strike.service import QuantStrikeService


class _FakeSource:
    def __init__(self, current: Decimal | None):
        self._cur = current
    def current(self) -> Decimal | None:
        return self._cur
    def realized_vol(self, window_s: int) -> float:
        return 0.0  # forces fixed-vol fallback


def test_evaluate_returns_none_for_unrecognized_question():
    svc = QuantStrikeService(sources={"BTC": _FakeSource(Decimal("60000"))})
    assert svc.evaluate("This is not a price question.", hours_to_resolution=24) is None


def test_evaluate_returns_thesis_for_btc_above():
    svc = QuantStrikeService(sources={"BTC": _FakeSource(Decimal("60000"))})
    out = svc.evaluate(
        "Will the price of Bitcoin be above $55,000 on Friday?",
        hours_to_resolution=24,
    )
    assert out is not None
    parsed, result, thesis = out
    assert parsed.asset_id == "BTC"
    assert 0.0 <= result.probability <= 1.0
    assert "BTC" in thesis


def test_evaluate_returns_none_when_no_spot():
    svc = QuantStrikeService(sources={"BTC": _FakeSource(None)})
    assert svc.evaluate(
        "Will the price of Bitcoin be above $55,000 on Friday?",
        hours_to_resolution=24,
    ) is None
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/unit/test_quant_strike_service.py -v`
Expected: FAIL — service module missing.

- [ ] **Step 3: Implement**

```python
# polyagent/services/quant/strike/service.py
"""Brain integration seam for strike-market questions.

Replaces CryptoQuantService with identical (matches/evaluate) surface.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from decimal import Decimal

from polyagent.services.quant.assets.registry import get
from polyagent.services.quant.assets.spec import MarketFamily
from polyagent.services.quant.assets.sources.base import PriceSource
from polyagent.services.quant.core.estimator import estimate_up_probability
from polyagent.services.quant.core.vol import compute_vol
from polyagent.services.quant.strike.parser import (
    ParsedStrike, StrikeKind, parse_question,
)

logger = logging.getLogger("polyagent.services.quant.strike")

_SECONDS_PER_YEAR = 365.25 * 24 * 3600


@dataclass(frozen=True)
class QuantResult:
    probability: float
    confidence: float
    sigma_distance: float


def _evaluate_strike(
    parsed: ParsedStrike, spot: Decimal, vol: float, hours_to_resolution: float,
) -> QuantResult:
    seconds = max(0.0, hours_to_resolution * 3600.0)
    p_above_low = estimate_up_probability(
        parsed.strike, spot, seconds, vol,
    )
    if parsed.kind == StrikeKind.UP:
        p = p_above_low
    elif parsed.kind == StrikeKind.DOWN:
        p = 1.0 - p_above_low
    else:
        assert parsed.upper_strike is not None
        p_above_high = estimate_up_probability(
            parsed.upper_strike, spot, seconds, vol,
        )
        p = max(0.0, p_above_low - p_above_high)
    days = hours_to_resolution / 24.0
    T = seconds / _SECONDS_PER_YEAR
    sigma_distance = 0.0
    if T > 0 and vol > 0 and float(parsed.strike) > 0 and float(spot) > 0:
        sigma_distance = abs(math.log(float(spot) / float(parsed.strike))) / (vol * math.sqrt(T))
    confidence = 0.95 if (days < 30 and sigma_distance < 3) else 0.70
    return QuantResult(probability=p, confidence=confidence, sigma_distance=sigma_distance)


def _build_thesis_text(
    parsed: ParsedStrike, spot: Decimal, vol: float, result: QuantResult,
) -> str:
    if parsed.kind == StrikeKind.UP:
        kind_desc = f"spot >= ${parsed.strike:,}"
    elif parsed.kind == StrikeKind.DOWN:
        kind_desc = f"spot < ${parsed.strike:,}"
    else:
        kind_desc = f"${parsed.strike:,} <= spot < ${parsed.upper_strike:,}"
    return (
        f"[quant_strike] {parsed.asset_id} {kind_desc} | "
        f"current_spot=${spot:,.2f} sigma={vol:.2f} "
        f"sigma_distance={result.sigma_distance:.2f} "
        f"P(YES)={result.probability:.4f} confidence={result.confidence:.2f} "
        f"(closed-form Φ(d₂), not LLM)"
    )


class QuantStrikeService:
    """Brain integration seam — replaces CryptoQuantService.

    Args:
        sources: dict mapping asset_id → PriceSource. The orchestrator
            owns the source instances; this service reads from them.
    """

    def __init__(self, sources: dict[str, PriceSource]) -> None:
        self._sources = sources

    def matches(self, question: str) -> ParsedStrike | None:
        return parse_question(question)

    def evaluate(
        self, question: str, hours_to_resolution: float
    ) -> tuple[ParsedStrike, QuantResult, str] | None:
        parsed = self.matches(question)
        if parsed is None:
            return None
        spec = get(parsed.asset_id)
        if spec is None or MarketFamily.STRIKE not in spec.supported_market_families:
            return None
        source = self._sources.get(parsed.asset_id)
        if source is None:
            logger.warning("quant_strike: no source for %s", parsed.asset_id)
            return None
        spot = source.current()
        if spot is None or spot <= 0:
            logger.warning("quant_strike: no spot for %s", parsed.asset_id)
            return None
        vol = compute_vol(spec, source, horizon_s=hours_to_resolution * 3600.0)
        result = _evaluate_strike(parsed, spot, vol, hours_to_resolution)
        thesis = _build_thesis_text(parsed, spot, vol, result)
        return parsed, result, thesis
```

- [ ] **Step 4: Now wire `__init__.py`**

```python
# polyagent/services/quant/strike/__init__.py — replace
"""Long-horizon strike-market handler. Brain integration seam."""
from polyagent.services.quant.strike.service import (
    ParsedStrike, QuantResult, QuantStrikeService, StrikeKind,
)

__all__ = ["QuantStrikeService", "QuantResult", "ParsedStrike", "StrikeKind"]
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/test_quant_strike_service.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add polyagent/services/quant/strike/service.py polyagent/services/quant/strike/__init__.py tests/unit/test_quant_strike_service.py
git commit -m "feat(quant): QuantStrikeService consumes registry"
```

### Task 4.3: Switch brain to `QuantStrikeService`

**Files:**
- Modify: `polyagent/services/brain.py`
- Modify: `polyagent/services/crypto_quant/__init__.py` (re-export from quant.strike for any stragglers)

- [ ] **Step 1: Identify the brain construction site**

Search:

```bash
grep -n "CryptoQuantService\|crypto_quant" polyagent/services/brain.py polyagent/main.py
```

- [ ] **Step 2: Update import + construction in `brain.py`**

In `polyagent/services/brain.py`, replace:

```python
from polyagent.services.crypto_quant import CryptoQuantService
```

with:

```python
from polyagent.services.quant.strike import QuantStrikeService as CryptoQuantService
```

(Keep the local alias `CryptoQuantService` so the rest of `brain.py` keeps using its existing name — minimal diff. Rename in a follow-up if desired.)

- [ ] **Step 3: Update construction in `polyagent/main.py`**

The current construction (find via grep) passes `btc_spot`, `eth_spot` and per-asset vols. The new constructor takes `sources={"BTC": btc_spot, "ETH": eth_spot}`. Replace the call:

```python
# Old:
crypto_quant = CryptoQuantService(
    btc_spot=btc_spot, eth_spot=eth_spot,
    btc_vol=settings.crypto_quant_btc_vol,
    eth_vol=settings.crypto_quant_eth_vol,
)

# New:
from polyagent.services.quant.strike import QuantStrikeService
crypto_quant = QuantStrikeService(sources={"BTC": btc_spot, "ETH": eth_spot})
```

(Per-asset vols now come from the registry; `crypto_quant_btc_vol` / `crypto_quant_eth_vol` env vars become `QUANT_BTC_VOL` / `QUANT_ETH_VOL` in PR 6.)

- [ ] **Step 4: Make `crypto_quant/__init__.py` a re-export shim**

```python
# polyagent/services/crypto_quant/__init__.py — replace entire file
"""Re-export shim. Real implementation lives in
polyagent.services.quant.strike. This module will be deleted in PR 6.
"""
from polyagent.services.quant.strike import (
    ParsedStrike, QuantResult, QuantStrikeService, StrikeKind,
)

# Back-compat alias used by brain.py before the rename.
CryptoQuantService = QuantStrikeService

__all__ = ["CryptoQuantService", "QuantStrikeService", "QuantResult", "ParsedStrike", "StrikeKind"]
```

- [ ] **Step 5: Delete the old `crypto_quant` source files**

```bash
git rm polyagent/services/crypto_quant/service.py
git rm polyagent/services/crypto_quant/parser.py
git rm polyagent/services/crypto_quant/estimator.py
```

- [ ] **Step 6: Run all tests including the brain regression suite**

Run: `pytest tests/ -v`
Expected: PASS — including any existing `test_brain*` or `test_crypto_quant*` tests. If tests reference `CryptoStrike`/`StrikeKind` symbols that no longer exist on the new module, update those tests to import from `polyagent.services.quant.strike` (`ParsedStrike`/`StrikeKind`) — keep behavior assertions intact.

- [ ] **Step 7: Commit**

```bash
git add polyagent/services/brain.py polyagent/main.py polyagent/services/crypto_quant/__init__.py
git commit -m "refactor(brain): swap CryptoQuantService for QuantStrikeService"
```

### Task 4.4: Push and PR

- [ ] **Step 1: Push**

```bash
git push -u origin refactor/quant-pr4-strike-migration
```

- [ ] **Step 2: Open PR**

Title: `refactor(quant) PR 4/6: crypto_quant → quant.strike`
Body: behavior-preserving for BTC + ETH strike markets; brain regression tests pass unchanged.

---

# PR 5 — Migration 006 + repository / model rename

**Goal:** Rename `btc5m_*` tables, repo class, and model classes. Add `asset_id` and `price_source_id` columns. Old class names remain as aliases until PR 6.

**Branch:** `refactor/quant-pr5-table-rename`

### Task 5.1: Migration 006 SQL

**Files:**
- Create: `db/migrations/006_quant_short_rename.sql`

- [ ] **Step 1: Write migration**

```sql
-- db/migrations/006_quant_short_rename.sql
-- Rename btc5m_* to quant_short_*; add asset_id and price_source_id.

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

- [ ] **Step 2: Test migration applies cleanly via the runner**

Run: `pytest tests/integration/test_migrate_runner.py -v -m integration` — existing tests cover the runner.

For the migration file itself, run an ad-hoc local test:

```bash
# Set up a fresh DB and apply 001-005, then 006:
POLYAGENT_TEST_DB_URL=postgresql://polyagent:polyagent@localhost:5432/postgres python -c "
import psycopg, uuid, os
db = f'polyagent_test_{uuid.uuid4().hex[:8]}'
admin = psycopg.connect(os.environ['POLYAGENT_TEST_DB_URL'], autocommit=True)
admin.cursor().execute(f'CREATE DATABASE \"{db}\"')
url = os.environ['POLYAGENT_TEST_DB_URL'].rsplit('/',1)[0] + f'/{db}'
conn = psycopg.connect(url)
conn.cursor().execute('CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\"')
conn.commit()
from polyagent.scripts.migrate import migrate_up
from pathlib import Path
applied = migrate_up(conn, Path('db/migrations'))
print('applied:', [m.version for m in applied])
cur = conn.cursor()
cur.execute(\"SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'quant_short%'\")
print('tables:', cur.fetchall())
admin.cursor().execute(f'DROP DATABASE \"{db}\" WITH (FORCE)')
"
```

Expected: `applied: ['001','002','003','004','005','006']` and `tables: [('quant_short_markets',),('quant_short_trades',)]`.

- [ ] **Step 3: Commit**

```bash
git add db/migrations/006_quant_short_rename.sql
git commit -m "feat(db): migration 006 renames btc5m_ tables to quant_short_"
```

### Task 5.2: Rename repository class with alias

**Files:**
- Modify: `polyagent/data/repositories/btc5m.py`
- Create: `polyagent/services/quant/short_horizon/__init__.py`
- Create: `polyagent/services/quant/short_horizon/repository.py`

- [ ] **Step 1: Move and rename the repository module**

```bash
git mv polyagent/data/repositories/btc5m.py polyagent/services/quant/short_horizon/repository.py
```

- [ ] **Step 2: Update class name + table refs in the moved file**

In `polyagent/services/quant/short_horizon/repository.py`:

- Rename class `Btc5mRepository` → `QuantShortRepository`.
- Replace all SQL string occurrences of `btc5m_markets` with `quant_short_markets` and `btc5m_trades` with `quant_short_trades`.
- Add `asset_id` to the SELECT column list of `SELECT_ACTIVE`, `SELECT_UNRESOLVED_PAST_END`, and any other queries that return market rows.
- Update `upsert_market` to insert `asset_id` (read from the model — added in Task 5.4).

Use grep to find all SQL strings:

```bash
grep -n "btc5m_" polyagent/services/quant/short_horizon/repository.py
```

Replace each, including any `RETURNING`/`ON CONFLICT` clauses. Make sure SQL test fixtures in `tests/unit/test_btc5m_repo.py` are updated correspondingly.

- [ ] **Step 3: Re-export the old class name from the old module location**

```python
# polyagent/data/repositories/btc5m.py — recreate as a tiny shim
"""Re-export shim. Real implementation lives in
polyagent.services.quant.short_horizon.repository. Deleted in PR 6.
"""
from polyagent.services.quant.short_horizon.repository import QuantShortRepository

# Back-compat alias used by btc5m worker until PR 6.
Btc5mRepository = QuantShortRepository

__all__ = ["QuantShortRepository", "Btc5mRepository"]
```

- [ ] **Step 4: Wire the short_horizon package init**

```python
# polyagent/services/quant/short_horizon/__init__.py
"""Short-horizon binary up/down market handler.

Decider, scanner, resolver land in PR 6.
"""
from polyagent.services.quant.short_horizon.repository import QuantShortRepository

__all__ = ["QuantShortRepository"]
```

- [ ] **Step 5: Update repository tests**

In `tests/unit/test_btc5m_repo.py` (or whatever the existing test file is named):

- Rename test file: `git mv tests/unit/test_btc5m_repo.py tests/unit/test_quant_short_repo.py`
- Update imports: `from polyagent.services.quant.short_horizon.repository import QuantShortRepository`
- Update SQL fixtures referencing `btc5m_*` to `quant_short_*`.
- Add an `asset_id` value where `upsert_market` is exercised.

- [ ] **Step 6: Run tests**

Run: `pytest tests/ -v -k "quant_short or btc5m"`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add polyagent/services/quant/short_horizon/__init__.py polyagent/services/quant/short_horizon/repository.py polyagent/data/repositories/btc5m.py tests/unit/test_quant_short_repo.py
git commit -m "refactor(quant): rename Btc5mRepository → QuantShortRepository"
```

### Task 5.3: Rename model classes with aliases

**Files:**
- Modify: `polyagent/models.py`

- [ ] **Step 1: Rename classes**

In `polyagent/models.py`:

- Rename `Btc5mMarket` → `QuantShortMarket`. Add an `asset_id: str` field (default `"BTC"` for back-compat with the worker that still constructs it without specifying asset_id).
- Rename `Btc5mTrade` → `QuantShortTrade`.
- Add aliases at the bottom of the file:

```python
# Back-compat aliases used by btc5m worker; deleted in PR 6.
Btc5mMarket = QuantShortMarket
Btc5mTrade = QuantShortTrade
```

- [ ] **Step 2: Update repository to populate `asset_id`**

In `polyagent/services/quant/short_horizon/repository.py`, the `upsert_market` method now has access to `market.asset_id`. The default of `"BTC"` keeps the existing btc5m worker behavior intact (it constructs `Btc5mMarket` instances which now alias to `QuantShortMarket(asset_id="BTC", ...)`).

- [ ] **Step 3: Run all tests**

Run: `pytest tests/ -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add polyagent/models.py polyagent/services/quant/short_horizon/repository.py
git commit -m "refactor(models): rename Btc5mMarket/Trade → QuantShortMarket/Trade"
```

### Task 5.4: Push + PR + deploy

- [ ] **Step 1: Push**

```bash
git push -u origin refactor/quant-pr5-table-rename
```

- [ ] **Step 2: PR**

Title: `refactor(quant) PR 5/6: migration 006 + repo/model rename`
Body notes: deploy will auto-apply 006 via `polyagent migrate up` (wired in PR 1).

- [ ] **Step 3: After merge + deploy, verify**

```bash
ssh algotrader@<host> 'polyagent migrate status'
```

Expected: `006_quant_short_rename.sql` listed under Applied.

```bash
ssh algotrader@<host> 'psql "$DATABASE_URL" -c "\dt quant_short*"'
```

Expected: lists `quant_short_markets`, `quant_short_trades`.

---

# PR 6 — Orchestrator + retire `btc5m/`

**Goal:** Replace `run_btc5m_worker` with `run_quant_orchestrator`. Build registry-aware scanner / decider / resolver. Delete `btc5m/` and `crypto_quant/` re-export shims. Rename env vars.

**Branch:** `refactor/quant-pr6-orchestrator`

### Task 6.1: Generalized slug scanner

**Files:**
- Create: `polyagent/services/quant/short_horizon/scanner.py`
- Create: `tests/unit/test_quant_short_scanner.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_quant_short_scanner.py
import json
from datetime import datetime, timezone

from polyagent.services.quant.short_horizon.scanner import (
    parse_short_horizon_slug, QuantShortScanner,
)


def test_parse_btc_5m_slug():
    end_ts = 1_900_000_000
    slug = f"btc-updown-5m-{end_ts}"
    asset_id, ws, we, dur = parse_short_horizon_slug(slug)
    assert asset_id == "BTC"
    assert dur == 300
    assert int(we.timestamp()) == end_ts
    assert int(ws.timestamp()) == end_ts - 300


def test_parse_eth_15m_slug():
    end_ts = 1_900_000_000
    slug = f"eth-updown-15m-{end_ts}"
    asset_id, ws, we, dur = parse_short_horizon_slug(slug)
    assert asset_id == "ETH"
    assert dur == 900


def test_parse_unknown_asset_returns_none():
    import pytest
    with pytest.raises(ValueError):
        parse_short_horizon_slug("doge-updown-5m-1900000000")


class _FakeHttp:
    def __init__(self, body):
        self._body = body
        self.last_params = None
    def get(self, url, params=None):
        self.last_params = params
        class R:
            status_code = 200
            def json(self_inner):
                return self_inner._body
            _body = self._body
        return R()


def test_scanner_returns_one_market_per_matching_slug():
    end_ts = 1_900_000_000
    body = [{
        "slug": f"btc-updown-5m-{end_ts}",
        "conditionId": "0xabc",
        "clobTokenIds": json.dumps(["yes_id", "no_id"]),
    }]
    s = QuantShortScanner(http_client=_FakeHttp(body))
    out = s.scan()
    assert len(out) == 1
    assert out[0].asset_id == "BTC"
    assert out[0].window_duration_s == 300
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/unit/test_quant_short_scanner.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# polyagent/services/quant/short_horizon/scanner.py
"""Registry-aware short-horizon slug scanner.

Matches `^(token1|token2|...)-updown-(\\d+[mhd])-(\\d+)$` where the token
union is built from registry.enabled_for(SHORT_HORIZON).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

from polyagent.models import QuantShortMarket
from polyagent.services.quant.assets.registry import enabled_for, get
from polyagent.services.quant.assets.spec import MarketFamily

logger = logging.getLogger("polyagent.services.quant.short_horizon.scanner")

_GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
_UNIT_TO_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def _build_slug_regex() -> re.Pattern[str]:
    tokens = [s.slug_token for s in enabled_for(MarketFamily.SHORT_HORIZON) if s.slug_token]
    if not tokens:
        # No assets enabled — match nothing.
        return re.compile(r"^$a")  # always-fail pattern
    union = "|".join(re.escape(t) for t in tokens)
    return re.compile(rf"^({union})-updown-(\d+[mhd])-(\d+)$")


def _duration_to_seconds(token: str) -> int:
    if not token or token[-1] not in _UNIT_TO_SECONDS:
        raise ValueError(f"bad duration token: {token!r}")
    n = int(token[:-1])
    if n <= 0:
        raise ValueError(f"non-positive duration: {token!r}")
    return n * _UNIT_TO_SECONDS[token[-1]]


def parse_short_horizon_slug(slug: str) -> tuple[str, datetime, datetime, int]:
    """Return (asset_id, window_start, window_end, duration_s).

    Raises ValueError on unknown asset or malformed slug.
    """
    pattern = _build_slug_regex()
    m = pattern.match(slug)
    if not m:
        raise ValueError(f"slug does not match any registered asset: {slug!r}")
    token, duration_token, end_unix = m.group(1), m.group(2), int(m.group(3))
    asset_id = next(
        (s.asset_id for s in enabled_for(MarketFamily.SHORT_HORIZON) if s.slug_token == token),
        None,
    )
    if asset_id is None:
        raise ValueError(f"no asset with slug_token={token!r}")
    duration_s = _duration_to_seconds(duration_token)
    window_end = datetime.fromtimestamp(end_unix, tz=timezone.utc)
    window_start = window_end - timedelta(seconds=duration_s)
    return asset_id, window_start, window_end, duration_s


class QuantShortScanner:
    """Polls Gamma for any registered short-horizon market."""

    def __init__(self, http_client=None, page_limit: int = 500) -> None:
        self._http = http_client or httpx.Client(timeout=15.0)
        self._page_limit = page_limit

    def scan(self) -> list[QuantShortMarket]:
        try:
            resp = self._http.get(
                _GAMMA_MARKETS_URL,
                params={"active": "true", "closed": "false", "limit": self._page_limit},
            )
            if resp.status_code != 200:
                logger.warning("gamma returned %s", resp.status_code)
                return []
            raw = resp.json()
        except Exception as exc:
            logger.warning("gamma fetch failed: %s", exc)
            return []
        out: list[QuantShortMarket] = []
        pattern = _build_slug_regex()
        for m in raw:
            slug = m.get("slug") or ""
            if not pattern.match(slug):
                continue
            try:
                asset_id, ws, we, dur = parse_short_horizon_slug(slug)
                token_ids = json.loads(m.get("clobTokenIds") or "[]")
                if len(token_ids) < 2:
                    continue
                out.append(QuantShortMarket(
                    polymarket_id=m.get("conditionId") or "",
                    slug=slug,
                    token_id_yes=token_ids[0],
                    token_id_no=token_ids[1],
                    window_duration_s=dur,
                    window_start_ts=ws,
                    window_end_ts=we,
                    asset_id=asset_id,
                ))
            except Exception as exc:
                logger.warning("parse failed for %s: %s", slug, exc)
                continue
        return out

    def close(self) -> None:
        self._http.close()
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_quant_short_scanner.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add polyagent/services/quant/short_horizon/scanner.py tests/unit/test_quant_short_scanner.py
git commit -m "feat(quant): registry-aware short-horizon scanner"
```

### Task 6.2: Generalized decider

**Files:**
- Create: `polyagent/services/quant/short_horizon/decider.py`
- Create: `tests/unit/test_quant_short_decider.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_quant_short_decider.py
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from polyagent.services.quant.short_horizon.decider import QuantDecider


class _FakeRepo:
    def __init__(self):
        self.trades_for: dict[str, list] = {}
        self.inserted: list = []
    def get_trades_for_market(self, market_id):
        return self.trades_for.get(market_id, [])
    def insert_trade(self, t):
        self.inserted.append(t)


class _FakeBook:
    def __init__(self, bid_ask):
        self._b = bid_ask
    def fetch_mid(self, token_id):
        return self._b


class _FakeSrc:
    def __init__(self, cur):
        self._c = cur
    def current(self):
        return self._c
    def realized_vol(self, window_s):
        return 0.0  # forces fixed fallback via HYBRID/long horizon


def _row(asset_id="BTC"):
    now = datetime.now(timezone.utc)
    return {
        "id": "m1",
        "polymarket_id": "0xabc",
        "asset_id": asset_id,
        "token_id_yes": "yes_id",
        "window_end_ts": now + timedelta(seconds=120),
        "start_spot": None,
    }


def test_decider_inserts_paper_trade_when_edge_clears():
    repo = _FakeRepo()
    sources = {"BTC": _FakeSrc(Decimal("60000"))}
    book = _FakeBook((Decimal("0.30"), Decimal("0.32")))  # mid 0.31
    d = QuantDecider(sources=sources, book=book, repo=repo, position_size_usd=Decimal("5"))
    d.evaluate(_row())
    # estimator at ATM with 60s ttm and HYBRID-fixed BTC vol → P(up) ≈ 0.50.
    # mid 0.31 → edge ~+0.19 > threshold 0.05 → YES trade
    assert len(repo.inserted) == 1
    assert repo.inserted[0].side == "YES"


def test_decider_skips_market_with_existing_trade():
    repo = _FakeRepo()
    repo.trades_for["m1"] = [{"id": "t1"}]
    sources = {"BTC": _FakeSrc(Decimal("60000"))}
    d = QuantDecider(sources=sources, book=_FakeBook((Decimal("0.4"), Decimal("0.5"))),
                     repo=repo, position_size_usd=Decimal("5"))
    d.evaluate(_row())
    assert repo.inserted == []


def test_decider_skips_when_no_spot():
    repo = _FakeRepo()
    sources = {"BTC": _FakeSrc(None)}
    d = QuantDecider(sources=sources, book=_FakeBook((Decimal("0.4"), Decimal("0.5"))),
                     repo=repo, position_size_usd=Decimal("5"))
    d.evaluate(_row())
    assert repo.inserted == []
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/unit/test_quant_short_decider.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# polyagent/services/quant/short_horizon/decider.py
"""Registry-aware decider for short-horizon binary markets.

For each active market row:
- Look up AssetSpec from registry by asset_id.
- Pull the matching PriceSource.
- Compute vol via compute_vol(spec, source, horizon_s).
- Run Φ(d₂); compute edge against book mid.
- Reject below spec.edge_threshold or below fees.
- Insert paper trade.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

from polyagent.models import QuantShortTrade
from polyagent.services.quant.assets.registry import apply_env_overrides, get
from polyagent.services.quant.assets.sources.base import PriceSource
from polyagent.services.quant.core.estimator import estimate_up_probability
from polyagent.services.quant.core.vol import compute_vol

logger = logging.getLogger("polyagent.services.quant.short_horizon.decider")


class BookFetcher(Protocol):
    def fetch_mid(self, token_id: str) -> tuple[Decimal, Decimal] | None: ...


class _RepoLike(Protocol):
    def get_trades_for_market(self, market_id: str) -> list[dict]: ...
    def insert_trade(self, trade) -> None: ...


class QuantDecider:
    def __init__(
        self,
        sources: dict[str, PriceSource],
        book: BookFetcher,
        repo: _RepoLike,
        position_size_usd: Decimal,
    ) -> None:
        self._sources = sources
        self._book = book
        self._repo = repo
        self._size = position_size_usd

    def evaluate(self, market_row: dict) -> None:
        market_id = market_row["id"]
        if self._repo.get_trades_for_market(market_id):
            return

        asset_id = market_row.get("asset_id") or "BTC"
        base_spec = get(asset_id)
        if base_spec is None:
            logger.warning("no spec for asset_id=%s, skipping market %s", asset_id, market_id)
            return
        spec = apply_env_overrides(base_spec)

        source = self._sources.get(asset_id)
        if source is None:
            return
        spot = source.current()
        if spot is None:
            return

        window_end = market_row["window_end_ts"]
        now = datetime.now(timezone.utc)
        ttm = (window_end - now).total_seconds()
        if ttm <= 0:
            return

        start_spot = market_row.get("start_spot") or spot
        vol = compute_vol(spec, source, horizon_s=ttm)
        p_up = estimate_up_probability(start_spot, spot, ttm, vol)

        book = self._book.fetch_mid(market_row["token_id_yes"])
        if book is None:
            return
        bid, ask = book
        mid = (float(bid) + float(ask)) / 2.0

        edge = p_up - mid
        if abs(edge) < spec.edge_threshold:
            return

        size_fraction = float(self._size)
        gross_edge_usd = abs(edge) * size_fraction
        fees_usd = size_fraction * spec.fee_bps / 10_000.0
        if gross_edge_usd <= fees_usd:
            return

        if edge > 0:
            side, fill = "YES", ask
        else:
            side, fill = "NO", bid

        trade = QuantShortTrade(
            market_id=market_id,
            side=side,
            fill_price_assumed=fill,
            size=self._size,
            estimator_p_up=p_up,
            spot_at_decision=spot,
            vol_at_decision=vol,
            edge_at_decision=edge,
        )
        self._repo.insert_trade(trade)
        logger.info(
            "PAPER %s on %s (%s): side=%s edge=%+.3f p_up=%.3f mid=%.3f",
            asset_id, market_row["polymarket_id"], asset_id, side, edge, p_up, mid,
        )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_quant_short_decider.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add polyagent/services/quant/short_horizon/decider.py tests/unit/test_quant_short_decider.py
git commit -m "feat(quant): registry-aware short-horizon decider"
```

### Task 6.3: Generalized resolver

**Files:**
- Create: `polyagent/services/quant/short_horizon/resolver.py`
- Create: `tests/unit/test_quant_short_resolver.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_quant_short_resolver.py
from datetime import datetime, timezone
from decimal import Decimal

from polyagent.services.quant.short_horizon.resolver import QuantResolver


class _FakeRepo:
    def __init__(self, markets):
        self.markets = markets
        self.resolved = []
        self.pnls = {}
    def get_unresolved_markets_past_end(self, now):
        return self.markets
    def update_market_resolution(self, market_id, *, start_spot, end_spot, outcome, price_source_id):
        self.resolved.append({
            "id": market_id, "start_spot": start_spot, "end_spot": end_spot,
            "outcome": outcome, "price_source_id": price_source_id,
        })
    def get_trades_for_market(self, market_id):
        return [{"id": "t1", "side": "YES", "fill_price_assumed": Decimal("0.40"),
                 "size": Decimal("5"), "pnl": None}]
    def update_trade_pnl(self, trade_id, pnl):
        self.pnls[trade_id] = pnl


class _FakeSettlement:
    def __init__(self, prices: dict, sid: str):
        self._p = prices
        self._sid = sid
    def price_at(self, ts):
        return self._p.get(ts)
    def source_id(self):
        return self._sid


def test_resolver_writes_outcome_and_pnl():
    ws = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    we = datetime(2026, 4, 1, 12, 5, tzinfo=timezone.utc)
    market = {
        "id": "m1", "polymarket_id": "0xabc", "asset_id": "BTC",
        "window_start_ts": ws, "window_end_ts": we,
    }
    repo = _FakeRepo([market])
    settlements = {"BTC": _FakeSettlement(
        {ws: Decimal("60000"), we: Decimal("60100")}, "coinbase:BTC-USD",
    )}
    r = QuantResolver(repo=repo, settlements=settlements)
    n = r.resolve_due_markets()
    assert n == 1
    assert repo.resolved[0]["outcome"] == "YES"
    assert repo.resolved[0]["price_source_id"] == "coinbase:BTC-USD"
    assert repo.pnls["t1"] == Decimal("5") * (Decimal("1") - Decimal("0.40"))


def test_resolver_skips_when_settlement_unavailable():
    ws = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    we = datetime(2026, 4, 1, 12, 5, tzinfo=timezone.utc)
    market = {"id": "m1", "polymarket_id": "0xabc", "asset_id": "BTC",
              "window_start_ts": ws, "window_end_ts": we}
    repo = _FakeRepo([market])
    settlements = {"BTC": _FakeSettlement({}, "coinbase:BTC-USD")}
    r = QuantResolver(repo=repo, settlements=settlements)
    assert r.resolve_due_markets() == 0
    assert repo.resolved == []
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/unit/test_quant_short_resolver.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# polyagent/services/quant/short_horizon/resolver.py
"""Registry-aware resolver. Writes price_source_id audit field."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

from polyagent.services.quant.assets.sources.base import SettlementSource
from polyagent.services.quant.core.pnl import compute_pnl

logger = logging.getLogger("polyagent.services.quant.short_horizon.resolver")


class _RepoLike(Protocol):
    def get_unresolved_markets_past_end(self, now: datetime) -> list[dict]: ...
    def update_market_resolution(
        self, market_id: str, *, start_spot, end_spot, outcome: str, price_source_id: str,
    ) -> None: ...
    def get_trades_for_market(self, market_id: str) -> list[dict]: ...
    def update_trade_pnl(self, trade_id: str, pnl) -> None: ...


class QuantResolver:
    def __init__(
        self,
        repo: _RepoLike,
        settlements: dict[str, SettlementSource],
    ) -> None:
        self._repo = repo
        self._settlements = settlements

    def resolve_due_markets(self) -> int:
        now = datetime.now(timezone.utc)
        markets = self._repo.get_unresolved_markets_past_end(now)
        resolved = 0
        for m in markets:
            asset_id = m.get("asset_id") or "BTC"
            settlement = self._settlements.get(asset_id)
            if settlement is None:
                logger.warning("no settlement source for asset_id=%s, skipping", asset_id)
                continue
            start_spot = settlement.price_at(m["window_start_ts"])
            end_spot = settlement.price_at(m["window_end_ts"])
            if start_spot is None or end_spot is None:
                logger.info("skip resolution of %s: spot history unavailable", m["polymarket_id"])
                continue
            outcome = "YES" if end_spot >= start_spot else "NO"
            self._repo.update_market_resolution(
                m["id"], start_spot=start_spot, end_spot=end_spot,
                outcome=outcome, price_source_id=settlement.source_id(),
            )
            for t in self._repo.get_trades_for_market(m["id"]):
                if t.get("pnl") is not None:
                    continue
                pnl = compute_pnl(
                    t["side"],
                    Decimal(str(t["fill_price_assumed"])),
                    outcome,
                    Decimal(str(t["size"])),
                )
                self._repo.update_trade_pnl(t["id"], pnl)
            resolved += 1
        if resolved:
            logger.info("resolved %d quant_short markets", resolved)
        return resolved
```

- [ ] **Step 4: Update repository to accept the new `price_source_id` parameter**

In `polyagent/services/quant/short_horizon/repository.py`, modify `update_market_resolution` to accept and persist `price_source_id`:

```python
# Update SQL:
UPDATE quant_short_markets
   SET start_spot = %(start_spot)s,
       end_spot = %(end_spot)s,
       outcome = %(outcome)s,
       resolved_at = NOW(),
       price_source_id = %(price_source_id)s
 WHERE id = %(id)s
```

And update the method signature. Tests in `test_quant_short_repo.py` get an updated `update_market_resolution` call passing `price_source_id="coinbase:BTC-USD"`.

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/test_quant_short_resolver.py tests/unit/test_quant_short_repo.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add polyagent/services/quant/short_horizon/resolver.py polyagent/services/quant/short_horizon/repository.py tests/unit/test_quant_short_resolver.py tests/unit/test_quant_short_repo.py
git commit -m "feat(quant): registry-aware resolver writes price_source_id"
```

### Task 6.4: Orchestrator

**Files:**
- Create: `polyagent/services/quant/orchestrator.py`
- Create: `tests/integration/test_quant_orchestrator.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_quant_orchestrator.py
import queue
import threading
import time
from decimal import Decimal

import pytest

from polyagent.services.quant.orchestrator import run_quant_orchestrator


class _FakeSpec:
    def __init__(self, asset_id, tick_interval_s=0.05):
        self.asset_id = asset_id
        self.tick_interval_s = tick_interval_s


class _FakeSrc:
    def __init__(self):
        self.ticks = 0
    def tick(self):
        self.ticks += 1
        return Decimal("100")
    def current(self):
        return Decimal("100") if self.ticks > 0 else None
    def realized_vol(self, window_s):
        return 0.0
    def close(self):
        pass


class _RaisingSrc(_FakeSrc):
    def tick(self):
        raise RuntimeError("upstream is down")


def test_orchestrator_isolates_failing_source():
    """Verify a raising source does not block the other source."""
    sources = {"BTC": _FakeSrc(), "ETH": _RaisingSrc()}
    specs = [_FakeSpec("BTC"), _FakeSpec("ETH")]
    shutdown = queue.Queue()

    def stop_after(secs):
        time.sleep(secs)
        shutdown.put("stop")

    threading.Thread(target=stop_after, args=(0.3,), daemon=True).start()

    run_quant_orchestrator(
        sources=sources,
        specs=specs,
        scan_and_decide=lambda: None,  # disabled for this test
        market_interval_s=10.0,
        shutdown_q=shutdown,
    )

    assert sources["BTC"].ticks > 0    # tick loop kept going
    # ETH raised on every tick attempt; we don't crash, we just log.
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/integration/test_quant_orchestrator.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# polyagent/services/quant/orchestrator.py
"""Single-thread quant orchestrator. Replaces run_btc5m_worker."""
from __future__ import annotations

import logging
import time
from typing import Callable

from polyagent.services.quant.assets.sources.base import PriceSource

logger = logging.getLogger("polyagent.services.quant.orchestrator")


def _safely(fn: Callable, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Exception:
        logger.exception("orchestrator: %s raised", getattr(fn, "__name__", repr(fn)))


def run_quant_orchestrator(
    sources: dict[str, PriceSource],
    specs: list,                          # iterable of objects with asset_id, tick_interval_s
    scan_and_decide: Callable[[], None],  # one call = scan + decide all assets + resolve
    market_interval_s: float,
    shutdown_q,
) -> None:
    """Long-running orchestrator. Returns when shutdown_q is non-empty.

    Per-asset tick cadence governed by spec.tick_interval_s. Market poll
    runs every market_interval_s. All per-asset operations wrapped in
    _safely so one failing source doesn't take down the others.
    """
    by_id = {s.asset_id: s for s in specs}
    last_tick_at = {asset_id: 0.0 for asset_id in sources}
    last_market_poll = 0.0
    min_tick_interval = min((s.tick_interval_s for s in specs), default=1.0)

    logger.info(
        "quant orchestrator started: assets=%s, market_poll=%ss, min_tick=%ss",
        sorted(sources.keys()), market_interval_s, min_tick_interval,
    )

    try:
        while shutdown_q.empty():
            now = time.time()
            for asset_id, src in sources.items():
                spec = by_id.get(asset_id)
                if spec is None:
                    continue
                if now - last_tick_at[asset_id] >= spec.tick_interval_s:
                    _safely(src.tick)
                    last_tick_at[asset_id] = now

            if now - last_market_poll >= market_interval_s:
                _safely(scan_and_decide)
                last_market_poll = now

            time.sleep(min_tick_interval)
    finally:
        for src in sources.values():
            _safely(src.close)
        logger.info("quant orchestrator stopped")
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/integration/test_quant_orchestrator.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add polyagent/services/quant/orchestrator.py tests/integration/test_quant_orchestrator.py
git commit -m "feat(quant): single-thread orchestrator with fault isolation"
```

### Task 6.5: Wire orchestrator into `polyagent/main.py`

**Files:**
- Modify: `polyagent/main.py`
- Modify: `polyagent/infra/config.py`

- [ ] **Step 1: Add the new settings + remove old ones**

In `polyagent/infra/config.py`:

- Remove fields: `btc5m_enabled`, `btc5m_spot_poll_s`, `btc5m_market_poll_s`, `btc5m_vol_window_s`, `btc5m_edge_threshold`, `btc5m_position_size_usd`, `btc5m_fees_bps`, `crypto_quant_enabled`, `crypto_quant_btc_vol`, `crypto_quant_eth_vol`, `crypto_quant_spot_poll_s`.
- Add: `quant_short_enabled: bool`, `quant_market_poll_s: int`, `quant_position_size_usd: float`.
- Update `Settings.from_env`:

```python
quant_short_enabled=_env_bool("QUANT_SHORT_ENABLED", False),
quant_market_poll_s=_env_int("QUANT_MARKET_POLL_S", 60),
quant_position_size_usd=_env_float("QUANT_POSITION_SIZE_USD", 5.0),
```

(Per-asset overrides — `QUANT_BTC_VOL`, etc. — read directly by `apply_env_overrides` in the registry, no Settings field needed.)

- [ ] **Step 2: Replace `run_btc5m_worker` invocation in `polyagent/main.py`**

Find the existing block:

```bash
grep -n "run_btc5m_worker\|btc5m_enabled" polyagent/main.py
```

Replace with:

```python
# polyagent/main.py — replace btc5m worker setup + invocation
from polyagent.services.quant.assets.registry import (
    ASSETS, apply_env_overrides, enabled_for,
)
from polyagent.services.quant.assets.spec import MarketFamily
from polyagent.services.quant.orchestrator import run_quant_orchestrator
from polyagent.services.quant.short_horizon.decider import QuantDecider
from polyagent.services.quant.short_horizon.repository import QuantShortRepository
from polyagent.services.quant.short_horizon.resolver import QuantResolver
from polyagent.services.quant.short_horizon.scanner import QuantShortScanner
from polyagent.services.quant.strike import QuantStrikeService

# ... inside run() ...

if not settings.quant_short_enabled:
    logger.info("quant short_horizon: disabled (set QUANT_SHORT_ENABLED=true)")
else:
    short_specs = [apply_env_overrides(s) for s in enabled_for(MarketFamily.SHORT_HORIZON)]
    sources = {s.asset_id: s.price_source() for s in short_specs}
    settlements = {s.asset_id: s.settlement_source() for s in short_specs}

    quant_repo = QuantShortRepository(db_pool)
    scanner = QuantShortScanner()
    book = PolymarketBookFetcher(polymarket_client)  # existing class — keep or move
    decider = QuantDecider(
        sources=sources, book=book, repo=quant_repo,
        position_size_usd=Decimal(str(settings.quant_position_size_usd)),
    )
    resolver = QuantResolver(repo=quant_repo, settlements=settlements)

    def scan_and_decide():
        try:
            for m in scanner.scan():
                quant_repo.upsert_market(m)
        except Exception:
            logger.exception("quant scan failed")
        try:
            for row in quant_repo.get_active_markets(datetime.now(timezone.utc)):
                decider.evaluate(row)
        except Exception:
            logger.exception("quant decider failed")
        try:
            resolver.resolve_due_markets()
        except Exception:
            logger.exception("quant resolver failed")

    quant_thread = threading.Thread(
        target=run_quant_orchestrator,
        kwargs=dict(
            sources=sources,
            specs=short_specs,
            scan_and_decide=scan_and_decide,
            market_interval_s=settings.quant_market_poll_s,
            shutdown_q=shutdown_q,
        ),
        name="quant-orchestrator",
        daemon=True,
    )
    quant_thread.start()

# Brain construction also changes: source dict that includes BOTH short and strike assets.
# Build a unified source dict from STRIKE assets too:
strike_sources = {
    s.asset_id: s.price_source() for s in enabled_for(MarketFamily.STRIKE)
    if s.asset_id not in (sources if settings.quant_short_enabled else {})
}
all_sources = {**(sources if settings.quant_short_enabled else {}), **strike_sources}
quant_strike = QuantStrikeService(sources=all_sources)
# Pass quant_strike where the brain expects its `crypto_quant` argument (rename optional).
```

> **Note:** the existing `polyagent/main.py` has more setup than shown — preserve the rest. The diff is: remove all `btc5m_*` setup; replace with the block above; ensure brain construction gets the new `quant_strike` instance.

- [ ] **Step 3: Run unit + integration tests**

Run: `pytest tests/ -v`
Expected: PASS for everything that does not require an actually-running bot. Some integration tests (`test_btc5m_stats_cli.py`) may need updates — see Task 6.6.

- [ ] **Step 4: Commit**

```bash
git add polyagent/main.py polyagent/infra/config.py
git commit -m "feat(quant): wire orchestrator + strike service into main"
```

### Task 6.6: Generalize `btc5m-stats` CLI → `quant-stats`

**Files:**
- Create: `polyagent/services/quant/cli/__init__.py`
- Create: `polyagent/services/quant/cli/stats.py`
- Modify: `polyagent/cli/main.py`
- Move: `tests/integration/test_btc5m_stats_cli.py` → `tests/integration/test_quant_stats_cli.py`
- Delete: `polyagent/cli/btc5m_stats.py`

- [ ] **Step 1: Move + adapt the stats command**

```python
# polyagent/services/quant/cli/__init__.py
"""Quant subsystem CLI commands."""
```

```python
# polyagent/services/quant/cli/stats.py
"""`polyagent quant-stats` — paper-trade summary across registered assets."""
from __future__ import annotations

import os
import click
import psycopg


@click.command("quant-stats")
@click.option("--asset", type=str, default=None,
              help="Filter to a single asset_id (e.g. BTC). Default: all.")
def quant_stats(asset: str | None) -> None:
    """Summary of quant_short paper trades, optionally filtered by asset."""
    url = os.environ["DATABASE_URL"]
    where = "WHERE m.asset_id = %s" if asset else ""
    params = (asset,) if asset else ()
    sql = f"""
        SELECT m.asset_id,
               COUNT(*) FILTER (WHERE t.pnl IS NOT NULL) AS resolved,
               COUNT(*) FILTER (WHERE t.pnl > 0) AS wins,
               COALESCE(SUM(t.pnl), 0) AS pnl
        FROM quant_short_trades t
        JOIN quant_short_markets m ON t.market_id = m.id
        {where}
        GROUP BY m.asset_id
        ORDER BY m.asset_id
    """
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    if not rows:
        click.echo("no trades yet")
        return
    click.echo(f"{'asset':<6} {'resolved':>10} {'wins':>6} {'pnl':>10}")
    for r in rows:
        win_rate = (r[2] / r[1] * 100) if r[1] else 0
        click.echo(f"{r[0]:<6} {r[1]:>10} {r[2]:>6} ({win_rate:5.1f}%) {float(r[3]):>9.2f}")
```

```python
# polyagent/cli/main.py — replace btc5m_stats import + registration
from polyagent.services.quant.cli.stats import quant_stats
# Remove: from polyagent.cli.btc5m_stats import btc5m_stats
# Remove: cli.add_command(btc5m_stats)
cli.add_command(quant_stats)
```

```bash
git rm polyagent/cli/btc5m_stats.py
git mv tests/integration/test_btc5m_stats_cli.py tests/integration/test_quant_stats_cli.py
```

Update the moved test: change CLI command name, update SQL fixtures to use `quant_short_*` tables and an `asset_id` value.

- [ ] **Step 2: Run tests**

Run: `pytest tests/integration/test_quant_stats_cli.py -v -m integration`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add polyagent/services/quant/cli/__init__.py polyagent/services/quant/cli/stats.py polyagent/cli/main.py polyagent/cli/btc5m_stats.py tests/integration/test_quant_stats_cli.py
git commit -m "feat(quant): generalize btc5m-stats → quant-stats with --asset filter"
```

### Task 6.7: Delete `btc5m/` and `crypto_quant/` re-export shims

**Files:**
- Delete entire `polyagent/services/btc5m/` directory.
- Delete entire `polyagent/services/crypto_quant/` directory.
- Delete `polyagent/data/repositories/btc5m.py`.

- [ ] **Step 1: Confirm nothing imports from the old paths**

```bash
grep -rn "from polyagent.services.btc5m\|from polyagent.services.crypto_quant\|from polyagent.data.repositories.btc5m" polyagent/ tests/
```

Expected: no matches. If any matches surface, update them to the new paths.

- [ ] **Step 2: Delete the directories**

```bash
git rm -r polyagent/services/btc5m
git rm -r polyagent/services/crypto_quant
git rm polyagent/data/repositories/btc5m.py
```

- [ ] **Step 3: Run all tests**

Run: `pytest tests/ -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git commit -m "refactor(quant): retire btc5m and crypto_quant packages"
```

### Task 6.8: Rename env vars in `.env.example`

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Update keys**

In `.env.example`:

- Remove: `BTC5M_*`, `CRYPTO_QUANT_*`.
- Add:

```
# Quant subsystem
QUANT_SHORT_ENABLED=false
QUANT_MARKET_POLL_S=60
QUANT_POSITION_SIZE_USD=5.0

# Per-asset overrides (optional — defaults are in
# polyagent/services/quant/assets/registry.py).
# QUANT_BTC_VOL=0.60
# QUANT_BTC_EDGE_THRESHOLD=0.05
# QUANT_BTC_FEE_BPS=0.0
# QUANT_BTC_PAPER_ONLY=false
# QUANT_ETH_VOL=0.75
# QUANT_ETH_EDGE_THRESHOLD=0.05
```

- [ ] **Step 2: Commit**

```bash
git add .env.example
git commit -m "chore(env): rename BTC5M_/CRYPTO_QUANT_ → QUANT_*"
```

### Task 6.9: Push, PR, deploy

- [ ] **Step 1: Push**

```bash
git push -u origin refactor/quant-pr6-orchestrator
```

- [ ] **Step 2: PR**

Title: `refactor(quant) PR 6/6: orchestrator + retire btc5m/crypto_quant`
Body lists deploy steps from `docs/refactor/quant-multi-asset-ops.md`:
1. Update `.env` on each host (rename keys per the ops doc).
2. Restart the bot.
3. Verify with `polyagent quant-stats` (will be empty until first markets land).
4. Tail logs for `quant orchestrator started: assets=['BTC', 'ETH']`.

---

## Cross-cutting Verification

After PR 6 ships, run the smoke battery on the deploy host:

- [ ] `polyagent migrate status` — all six migrations Applied, none Drifted.
- [ ] `polyagent quant-stats` — runs without error (likely empty).
- [ ] `journalctl -u polyagent | tail -50` — orchestrator startup log present, no exceptions.
- [ ] After 5 minutes: `polyagent quant-stats` — markets either still empty (no Polymarket BTC up/down listings) or showing rows tagged with `asset_id`.
- [ ] After 1 hour: brain logs show at least one `[quant_strike]` thesis on a strike-market evaluation.

Adding a new asset (e.g. SOL) is then:
1. Add `"SOL": AssetSpec(...)` to `polyagent/services/quant/assets/registry.py`.
2. If SOL needs a non-Coinbase price source, add it under `polyagent/services/quant/assets/sources/`.
3. Add tests in `tests/unit/test_quant_assets_registry.py`.
4. Ship as a single PR.

---

## Self-Review Notes

This plan was self-reviewed against the spec (`docs/refactor/quant-multi-asset.md`):

- **Spec coverage:** every section of the spec maps to at least one task. PR 1 covers the migration runner; PR 2-3 the type/source/registry foundations; PR 4 the strike seam; PR 5 the table rename; PR 6 the orchestrator and removals.
- **No placeholders:** no `TBD`, `TODO`, "implement later", or "similar to Task N" references. Every code step contains the actual code.
- **Type consistency:** `AssetSpec`, `VolCalibration`, `MarketFamily`, `PriceSource`, `SettlementSource` field/method names are consistent across PRs 2-6. `QuantShortMarket`/`QuantShortTrade`/`QuantShortRepository` named consistently from PR 5 onward. `QuantStrikeService.evaluate(question, hours_to_resolution)` and `ParsedStrike` shape are used consistently in PR 4 and the brain wiring.

Two notes for engineers executing this plan:

1. **Tests in the existing repo may have names this plan does not enumerate.** When PR 5 renames model/repo classes, run `grep -rn "Btc5m" tests/` to find every fixture, import, or assertion that needs updating. Do not assume the test layout matches what's listed here verbatim.
2. **The `PolymarketBookFetcher` class lives in `polyagent/services/btc5m/worker.py` today.** When PR 6 deletes `btc5m/`, move `PolymarketBookFetcher` to `polyagent/services/quant/short_horizon/book.py` first (or fold it into `polyagent/main.py` near the construction site). This is a small lift but is not called out as its own task — handle it in Task 6.5 or 6.7.

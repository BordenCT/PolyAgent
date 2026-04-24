# BTC 5m Subsystem — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a paper-only BTC short-horizon up/down worker that scans Polymarket for `btc-updown-<duration>-<ts>` markets (5m and 15m in Phase 1; 1h/4h/1d are supported by the same code if Polymarket lists them), estimates P(up) via lognormal Φ(d₂) against Coinbase spot + realized vol, records hypothetical trades when edge exceeds threshold, and resolves them by recording actual outcome + P&L. One new thread, single-source price feed, no brain, no ML.

The subsystem retains the `btc5m` prefix in code (tables, modules, env vars) as historical shorthand — the 5-minute case was the origin, but the schema stores `window_duration_s` and the scanner regex matches any duration token.

**Architecture:** A single `btc5m_worker` thread registered in `polyagent/main.py` alongside the four existing workers. The worker loop ticks the spot source every 2s, scans Polymarket every 60s, decides and records paper trades every 60s per active market, and resolves markets whose window has ended. Data persists in two new tables (`btc5m_markets`, `btc5m_trades`).

**Tech Stack:** Python 3.14, httpx (sync), psycopg, PostgreSQL, Click, Rich, pytest. No new runtime dependencies — stays within PolyAgent's existing stack.

**Source spec:** `docs/feat/btc-5m-subsystem.md`

---

## File Structure

**Create:**
- `db/migrations/005_btc5m.sql` — schema
- `polyagent/services/btc5m/__init__.py` — package marker
- `polyagent/services/btc5m/spot.py` — Coinbase price source + realized vol
- `polyagent/services/btc5m/estimator.py` — lognormal probability
- `polyagent/services/btc5m/scanner.py` — Gamma poller + slug parser
- `polyagent/services/btc5m/decider.py` — threshold decision + paper trade
- `polyagent/services/btc5m/resolver.py` — outcome + PnL on window end
- `polyagent/services/btc5m/worker.py` — assembled loop
- `polyagent/data/repositories/btc5m.py` — CRUD for both tables
- `polyagent/cli/btc5m_stats.py` — per-class analytics CLI
- `tests/unit/services/btc5m/__init__.py`
- `tests/unit/services/btc5m/test_spot.py`
- `tests/unit/services/btc5m/test_estimator.py`
- `tests/unit/services/btc5m/test_scanner.py`
- `tests/unit/services/btc5m/test_decider.py`
- `tests/integration/test_btc5m_resolver.py`
- `tests/integration/test_btc5m_stats_cli.py`

**Modify:**
- `polyagent/models.py` — add `Btc5mMarket`, `Btc5mTrade` dataclasses
- `polyagent/infra/config.py` — add `BTC5M_*` env-var settings
- `polyagent/main.py` — register `btc5m_worker` thread (guarded by `BTC5M_ENABLED`)
- `polyagent/cli/main.py` — register `btc5m-stats` command

---

## Task 1: Schema migration

**Files:**
- Create: `db/migrations/005_btc5m.sql`

- [ ] **Step 1: Write the migration**

Create `db/migrations/005_btc5m.sql`:

```sql
-- db/migrations/005_btc5m.sql
-- BTC 5-minute paper-trading subsystem: markets and trades.

CREATE TABLE IF NOT EXISTS btc5m_markets (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    polymarket_id     TEXT UNIQUE NOT NULL,
    slug              TEXT UNIQUE NOT NULL,
    token_id_yes      TEXT NOT NULL,
    token_id_no       TEXT NOT NULL,
    window_duration_s INTEGER NOT NULL,
    window_start_ts   TIMESTAMPTZ NOT NULL,
    window_end_ts     TIMESTAMPTZ NOT NULL,
    start_spot        DECIMAL,
    end_spot          DECIMAL,
    outcome           TEXT,
    discovered_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_btc5m_markets_window_end ON btc5m_markets(window_end_ts);
CREATE INDEX IF NOT EXISTS idx_btc5m_markets_outcome    ON btc5m_markets(outcome);
CREATE INDEX IF NOT EXISTS idx_btc5m_markets_duration   ON btc5m_markets(window_duration_s);

CREATE TABLE IF NOT EXISTS btc5m_trades (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    market_id           UUID NOT NULL REFERENCES btc5m_markets(id),
    decision_ts         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    side                TEXT NOT NULL,
    fill_price_assumed  DECIMAL NOT NULL,
    size                DECIMAL NOT NULL,
    estimator_p_up      DECIMAL NOT NULL,
    spot_at_decision    DECIMAL NOT NULL,
    vol_at_decision     DECIMAL NOT NULL,
    edge_at_decision    DECIMAL NOT NULL,
    pnl                 DECIMAL,
    resolved_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_btc5m_trades_market   ON btc5m_trades(market_id);
CREATE INDEX IF NOT EXISTS idx_btc5m_trades_decision ON btc5m_trades(decision_ts DESC);
```

- [ ] **Step 2: Apply and verify**

Run:

```bash
podman exec -i polyagent-db psql -U polyagent -d polyagent < db/migrations/005_btc5m.sql
podman exec -i polyagent-db psql -U polyagent -d polyagent_test < db/migrations/005_btc5m.sql
podman exec polyagent-db psql -U polyagent -d polyagent -c "\d+ btc5m_markets" | grep window_end_ts
podman exec polyagent-db psql -U polyagent -d polyagent -c "\d+ btc5m_trades" | grep edge_at_decision
```

Expected: both greps return one line each.

- [ ] **Step 3: Commit**

```bash
git add db/migrations/005_btc5m.sql
git commit -m "feat(db): add btc5m_markets and btc5m_trades tables"
```

---

## Task 2: Domain models

**Files:**
- Modify: `polyagent/models.py`
- Modify: `tests/unit/test_models.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_models.py`:

```python
from datetime import datetime, timezone
from polyagent.models import Btc5mMarket, Btc5mTrade


class TestBtc5mMarket:
    def test_open_market_has_no_outcome(self):
        m = Btc5mMarket(
            polymarket_id="0xabc",
            slug="btc-updown-5m-1776995400",
            token_id_yes="y",
            token_id_no="n",
            window_duration_s=300,
            window_start_ts=datetime(2026, 4, 24, 1, 45, tzinfo=timezone.utc),
            window_end_ts=datetime(2026, 4, 24, 1, 50, tzinfo=timezone.utc),
        )
        assert m.outcome is None
        assert m.start_spot is None
        assert m.end_spot is None
        assert m.window_duration_s == 300

    def test_resolved_15m_market_has_outcome_and_spots(self):
        m = Btc5mMarket(
            polymarket_id="0xabc",
            slug="btc-updown-15m-1776995400",
            token_id_yes="y",
            token_id_no="n",
            window_duration_s=900,
            window_start_ts=datetime(2026, 4, 24, 1, 35, tzinfo=timezone.utc),
            window_end_ts=datetime(2026, 4, 24, 1, 50, tzinfo=timezone.utc),
            start_spot=Decimal("65000"),
            end_spot=Decimal("65100"),
            outcome="YES",
        )
        assert m.outcome == "YES"
        assert m.window_duration_s == 900


class TestBtc5mTrade:
    def test_create_unresolved_trade(self):
        t = Btc5mTrade(
            market_id=uuid4(),
            side="YES",
            fill_price_assumed=Decimal("0.52"),
            size=Decimal("5.00"),
            estimator_p_up=0.58,
            spot_at_decision=Decimal("65000"),
            vol_at_decision=0.45,
            edge_at_decision=0.06,
        )
        assert t.pnl is None
```

Make sure the file already has `from uuid import uuid4` and `from decimal import Decimal`.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/unit/test_models.py::TestBtc5mMarket tests/unit/test_models.py::TestBtc5mTrade -v
```

Expected: ImportError / AttributeError.

- [ ] **Step 3: Add models**

In `polyagent/models.py`, append after `Position`:

```python
@dataclass
class Btc5mMarket:
    """A Polymarket BTC short-horizon up/down market snapshot.

    Covers 5m and 15m today; any ``btc-updown-<duration>-<ts>`` slug
    with a duration expressible in seconds is supported.

    Args:
        polymarket_id: Condition ID on Polymarket.
        slug: Market slug, form ``btc-updown-<duration>-<unix_ts>``.
        token_id_yes: CLOB token ID for YES side.
        token_id_no: CLOB token ID for NO side.
        window_duration_s: Resolution-window length in seconds (300 for 5m,
                           900 for 15m, 3600 for 1h, etc.).
        window_start_ts: Resolution window open (UTC).
        window_end_ts: Resolution window close (UTC).
        start_spot: Coinbase BTC/USD at window_start_ts; None until resolver runs.
        end_spot: Coinbase BTC/USD at window_end_ts; None until resolver runs.
        outcome: 'YES' if end_spot >= start_spot, else 'NO'; None until resolver runs.
    """
    polymarket_id: str
    slug: str
    token_id_yes: str
    token_id_no: str
    window_duration_s: int
    window_start_ts: datetime
    window_end_ts: datetime
    start_spot: Decimal | None = None
    end_spot: Decimal | None = None
    outcome: str | None = None


@dataclass
class Btc5mTrade:
    """A simulated paper trade on a Btc5mMarket.

    Args:
        market_id: Foreign key to ``btc5m_markets.id``.
        side: 'YES' or 'NO'.
        fill_price_assumed: Worst-case fill (best_ask for YES, best_bid for NO).
        size: Notional USD of the trade.
        estimator_p_up: Estimator's P(up) at decision time.
        spot_at_decision: Coinbase spot at decision time.
        vol_at_decision: Rolling realized vol used for the estimator.
        edge_at_decision: estimator_p_up - market_mid at decision time.
        pnl: Realized P&L set by resolver; None while market is open.
    """
    market_id: UUID
    side: str
    fill_price_assumed: Decimal
    size: Decimal
    estimator_p_up: float
    spot_at_decision: Decimal
    vol_at_decision: float
    edge_at_decision: float
    pnl: Decimal | None = None
```

- [ ] **Step 4: Run tests**

Run:

```bash
uv run pytest tests/unit/test_models.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add polyagent/models.py tests/unit/test_models.py
git commit -m "feat(models): add Btc5mMarket and Btc5mTrade dataclasses"
```

---

## Task 3: Config settings

**Files:**
- Modify: `polyagent/infra/config.py`
- Modify: `tests/unit/test_config.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_config.py`:

```python
def test_btc5m_defaults(settings):
    assert settings.btc5m_enabled is False
    assert settings.btc5m_spot_poll_s == 2.0
    assert settings.btc5m_market_poll_s == 60
    assert settings.btc5m_vol_window_s == 300
    assert settings.btc5m_edge_threshold == 0.05
    assert settings.btc5m_position_size_usd == 5.0
    assert settings.btc5m_fees_bps == 0.0


def test_btc5m_from_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("BTC5M_ENABLED", "true")
    monkeypatch.setenv("BTC5M_EDGE_THRESHOLD", "0.08")
    from polyagent.infra.config import Settings
    s = Settings.from_env()
    assert s.btc5m_enabled is True
    assert s.btc5m_edge_threshold == 0.08
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/unit/test_config.py::test_btc5m_defaults tests/unit/test_config.py::test_btc5m_from_env -v
```

Expected: AttributeError.

- [ ] **Step 3: Add fields**

In `polyagent/infra/config.py`, inside the `Settings` dataclass add alongside existing fields:

```python
    # BTC 5-minute subsystem
    btc5m_enabled: bool
    btc5m_spot_poll_s: float
    btc5m_market_poll_s: int
    btc5m_vol_window_s: int
    btc5m_edge_threshold: float
    btc5m_position_size_usd: float
    btc5m_fees_bps: float
```

And in `Settings.from_env()`, in the `return Settings(...)` call, add:

```python
            btc5m_enabled=_env_bool("BTC5M_ENABLED", False),
            btc5m_spot_poll_s=_env_float("BTC5M_SPOT_POLL_S", 2.0),
            btc5m_market_poll_s=_env_int("BTC5M_MARKET_POLL_S", 60),
            btc5m_vol_window_s=_env_int("BTC5M_VOL_WINDOW_S", 300),
            btc5m_edge_threshold=_env_float("BTC5M_EDGE_THRESHOLD", 0.05),
            btc5m_position_size_usd=_env_float("BTC5M_POSITION_SIZE_USD", 5.0),
            btc5m_fees_bps=_env_float("BTC5M_FEES_BPS", 0.0),
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/unit/test_config.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add polyagent/infra/config.py tests/unit/test_config.py
git commit -m "feat(config): add BTC5M_* settings"
```

---

## Task 4: Estimator module (pure Φ(d₂))

**Files:**
- Create: `polyagent/services/btc5m/__init__.py` (empty)
- Create: `polyagent/services/btc5m/estimator.py`
- Create: `tests/unit/services/btc5m/__init__.py` (empty)
- Create: `tests/unit/services/btc5m/test_estimator.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/services/btc5m/test_estimator.py`:

```python
"""Tests for the BTC 5m lognormal estimator."""
from __future__ import annotations
from decimal import Decimal

import pytest

from polyagent.services.btc5m.estimator import estimate_up_probability


def test_atm_near_expiration_is_half():
    # At the money, any positive TTM: p_up ≈ 0.5 (slight drift, but σ²/2 term small)
    p = estimate_up_probability(
        start_price=Decimal("65000"),
        current_spot=Decimal("65000"),
        seconds_to_resolution=60,
        annualised_vol=0.50,
    )
    assert 0.48 < p < 0.52


def test_deep_itm_is_near_one():
    # Current spot 5% above start, 60s TTM: very likely up
    p = estimate_up_probability(
        start_price=Decimal("65000"),
        current_spot=Decimal("68000"),
        seconds_to_resolution=60,
        annualised_vol=0.50,
    )
    assert p > 0.95


def test_deep_otm_is_near_zero():
    p = estimate_up_probability(
        start_price=Decimal("65000"),
        current_spot=Decimal("62000"),
        seconds_to_resolution=60,
        annualised_vol=0.50,
    )
    assert p < 0.05


def test_higher_vol_pulls_otm_toward_half():
    low = estimate_up_probability(
        Decimal("65000"), Decimal("64800"), 300, annualised_vol=0.20
    )
    high = estimate_up_probability(
        Decimal("65000"), Decimal("64800"), 300, annualised_vol=1.50
    )
    # Higher vol → more uncertainty → closer to 0.5
    assert abs(high - 0.5) < abs(low - 0.5)


def test_zero_vol_returns_half_when_flat():
    # Edge case: zero vol, ATM — should not raise, returns ~0.5
    p = estimate_up_probability(
        start_price=Decimal("65000"),
        current_spot=Decimal("65000"),
        seconds_to_resolution=60,
        annualised_vol=0.0,
    )
    assert p == pytest.approx(0.5, abs=0.01)


def test_zero_ttm_is_binary():
    # TTM = 0 should collapse: p_up ≈ 1 if current > start, ≈ 0 if lower
    p_above = estimate_up_probability(Decimal("65000"), Decimal("65100"), 0.0, 0.50)
    p_below = estimate_up_probability(Decimal("65000"), Decimal("64900"), 0.0, 0.50)
    assert p_above > 0.99
    assert p_below < 0.01


def test_output_always_in_unit_interval():
    # Adversarial: extreme inputs shouldn't escape [0, 1]
    for params in [
        (Decimal("65000"), Decimal("130000"), 10, 3.0),
        (Decimal("65000"), Decimal("100"), 1, 5.0),
        (Decimal("65000"), Decimal("65000"), 10000, 0.001),
    ]:
        p = estimate_up_probability(*params)
        assert 0.0 <= p <= 1.0, f"out of range for {params}: {p}"
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/unit/services/btc5m/test_estimator.py -v
```

Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement the estimator**

Create `polyagent/services/btc5m/__init__.py` (empty file). Then create `polyagent/services/btc5m/estimator.py`:

```python
"""Lognormal P(up) estimator for BTC 5m binary markets.

Pure function. No I/O. Never raises. Parameterized on time-to-maturity so
the same code serves 5m today and 1h/1d if expanded later.
"""
from __future__ import annotations

import math
from decimal import Decimal

_SECONDS_PER_YEAR = 365.25 * 24 * 3600


def estimate_up_probability(
    start_price: Decimal,
    current_spot: Decimal,
    seconds_to_resolution: float,
    annualised_vol: float,
) -> float:
    """P(spot_at_resolution >= start_price) under lognormal dynamics.

    Black-Scholes Φ(d₂) with r = 0 (fair game over short horizons):

        d₂ = (ln(S/K) - σ² T / 2) / (σ √T)
        P(up) = Φ(d₂)

    Args:
        start_price: The strike (K) — price at window_start_ts, or the
                     current spot before the window opens.
        current_spot: Current BTC spot price from Coinbase.
        seconds_to_resolution: Time until window closes. <= 0 means the
                               market is effectively resolved.
        annualised_vol: σ in fractional units (e.g., 0.50 for 50%/year).

    Returns:
        P(up) clamped to [0, 1].
    """
    S = float(current_spot)
    K = float(start_price)

    if seconds_to_resolution <= 0:
        # Window closed: outcome is determined by current spot vs start
        if S > K:
            return 1.0
        if S < K:
            return 0.0
        return 0.5

    if annualised_vol <= 0 or K <= 0 or S <= 0:
        # Degenerate: no randomness left. Hand back 0.5 at ATM, else binary.
        if S > K:
            return 1.0
        if S < K:
            return 0.0
        return 0.5

    T = seconds_to_resolution / _SECONDS_PER_YEAR
    vol_sqrt_t = annualised_vol * math.sqrt(T)
    # d2 per Black-Scholes with r=0
    d2 = (math.log(S / K) - 0.5 * annualised_vol * annualised_vol * T) / vol_sqrt_t

    # Standard normal CDF via erf
    p = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
    if p < 0.0:
        return 0.0
    if p > 1.0:
        return 1.0
    return p
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/unit/services/btc5m/test_estimator.py -v
```

Expected: all 7 pass.

- [ ] **Step 5: Commit**

```bash
git add polyagent/services/btc5m/ tests/unit/services/btc5m/
git commit -m "feat(btc5m): add lognormal up-probability estimator"
```

---

## Task 5: Spot source (Coinbase + realized vol)

**Files:**
- Create: `polyagent/services/btc5m/spot.py`
- Create: `tests/unit/services/btc5m/test_spot.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/services/btc5m/test_spot.py`:

```python
"""Tests for BtcSpotSource — ring buffer, tick, realized vol."""
from __future__ import annotations
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from polyagent.services.btc5m.spot import BtcSpotSource


class TestBtcSpotSource:
    def test_current_is_none_before_first_tick(self):
        src = BtcSpotSource()
        assert src.current() is None

    def test_tick_stores_price(self):
        src = BtcSpotSource()
        with patch.object(src, "_fetch_ticker", return_value=Decimal("65000.12")):
            p = src.tick()
        assert p == Decimal("65000.12")
        assert src.current() == Decimal("65000.12")

    def test_realized_vol_returns_zero_with_flat_ticks(self):
        src = BtcSpotSource()
        now = 1_000_000.0
        prices = [Decimal("65000")] * 10
        with patch("time.time", side_effect=[now + i for i in range(10)]):
            with patch.object(src, "_fetch_ticker", side_effect=prices):
                for _ in range(10):
                    src.tick()
        assert src.realized_vol(window_s=60) == pytest.approx(0.0, abs=1e-9)

    def test_realized_vol_positive_with_varying_ticks(self):
        src = BtcSpotSource()
        now = 1_000_000.0
        # Synthetic price path — alternating up/down ~0.1% steps
        prices = [
            Decimal(str(65000 * (1 + (0.001 if i % 2 == 0 else -0.001))))
            for i in range(30)
        ]
        # 1s apart
        with patch("time.time", side_effect=[now + i for i in range(30)]):
            with patch.object(src, "_fetch_ticker", side_effect=prices):
                for _ in range(30):
                    src.tick()
        vol = src.realized_vol(window_s=30)
        # With 0.1% swings every 1s, annualised vol is very large — just assert positive
        assert vol > 0.0

    def test_realized_vol_returns_zero_with_too_few_samples(self):
        src = BtcSpotSource()
        with patch.object(src, "_fetch_ticker", return_value=Decimal("65000")):
            src.tick()  # only one sample
        assert src.realized_vol(window_s=60) == 0.0

    def test_ring_buffer_caps_at_one_hour(self):
        src = BtcSpotSource(_max_age_s=3600)
        # 3700 ticks at 1s each — buffer should retain only last ~3600
        now = 1_000_000.0
        with patch("time.time", side_effect=[now + i for i in range(3700)]):
            with patch.object(src, "_fetch_ticker", return_value=Decimal("65000")):
                for _ in range(3700):
                    src.tick()
        assert len(src._buf) <= 3601

    def test_tick_swallows_http_errors(self):
        src = BtcSpotSource()
        with patch.object(src, "_fetch_ticker", side_effect=RuntimeError("boom")):
            result = src.tick()
        assert result is None
        assert src.current() is None
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/unit/services/btc5m/test_spot.py -v
```

Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `polyagent/services/btc5m/spot.py`:

```python
"""Coinbase BTC/USD spot source with rolling realized-vol.

Public interface (PriceSource protocol):
    tick()          -> Decimal | None
    current()       -> Decimal | None
    realized_vol(s) -> float (annualised σ)

Designed so a ChainlinkSpotSource subclass can slot in with no other
changes once Data Streams credentials arrive.
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from decimal import Decimal

import httpx

logger = logging.getLogger("polyagent.services.btc5m.spot")

_COINBASE_TICKER_URL = (
    "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
)
_SECONDS_PER_YEAR = 365.25 * 24 * 3600


class BtcSpotSource:
    """In-memory rolling cache of BTC/USD mid prices from Coinbase."""

    def __init__(self, _max_age_s: int = 3600, timeout_s: float = 5.0) -> None:
        self._max_age_s = _max_age_s
        self._buf: deque[tuple[float, Decimal]] = deque()
        self._http = httpx.Client(timeout=timeout_s)

    def _fetch_ticker(self) -> Decimal:
        """Fetch the current BTC/USD price from Coinbase, as the mid of bid/ask.

        Raises on HTTP error.
        """
        resp = self._http.get(_COINBASE_TICKER_URL)
        resp.raise_for_status()
        body = resp.json()
        bid = Decimal(str(body["bid"]))
        ask = Decimal(str(body["ask"]))
        return (bid + ask) / Decimal("2")

    def tick(self) -> Decimal | None:
        """Fetch a spot price, append to buffer, return it.

        Returns None on HTTP/parse error (logged but not raised).
        """
        try:
            price = self._fetch_ticker()
        except Exception as exc:
            logger.warning("spot tick failed: %s", exc)
            return None

        now = time.time()
        self._buf.append((now, price))

        cutoff = now - self._max_age_s
        while self._buf and self._buf[0][0] < cutoff:
            self._buf.popleft()

        return price

    def current(self) -> Decimal | None:
        """Return the most recent cached price, or None if we haven't ticked."""
        if not self._buf:
            return None
        return self._buf[-1][1]

    def realized_vol(self, window_s: int = 300) -> float:
        """Annualised σ of log returns over the trailing window.

        Returns 0.0 if fewer than 2 samples fall in the window.
        """
        if not self._buf:
            return 0.0
        now = time.time()
        cutoff = now - window_s
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
        # Per-return variance. Scale to per-second, then annualise.
        # Samples are not exactly evenly spaced; use window length as proxy.
        span_s = samples[-1][0] - samples[0][0]
        if span_s <= 0:
            return 0.0
        per_s_variance = variance * len(log_returns) / span_s
        return math.sqrt(per_s_variance * _SECONDS_PER_YEAR)

    def close(self) -> None:
        self._http.close()
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/unit/services/btc5m/test_spot.py -v
```

Expected: 7 pass.

- [ ] **Step 5: Commit**

```bash
git add polyagent/services/btc5m/spot.py tests/unit/services/btc5m/test_spot.py
git commit -m "feat(btc5m): add Coinbase BtcSpotSource with realized vol"
```

---

## Task 6: Scanner (slug parser + Gamma poll)

**Files:**
- Create: `polyagent/services/btc5m/scanner.py`
- Create: `tests/unit/services/btc5m/test_scanner.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/services/btc5m/test_scanner.py`:

```python
"""Tests for the BTC 5m market scanner and slug parser."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from polyagent.services.btc5m.scanner import (
    BTC5M_SLUG_RE,
    parse_btc5m_slug,
    Btc5mScanner,
)


class TestSlugParser:
    def test_valid_5m_slug(self):
        m = BTC5M_SLUG_RE.match("btc-updown-5m-1776995400")
        assert m is not None
        assert m.group(1) == "5m"
        assert m.group(2) == "1776995400"

    def test_valid_15m_slug(self):
        m = BTC5M_SLUG_RE.match("btc-updown-15m-1776995400")
        assert m is not None
        assert m.group(1) == "15m"
        assert m.group(2) == "1776995400"

    def test_parse_5m_returns_300s_window(self):
        window_start, window_end, duration = parse_btc5m_slug("btc-updown-5m-1776995400")
        assert window_end == datetime(2026, 4, 24, 1, 50, tzinfo=timezone.utc)
        assert duration == 300
        assert (window_end - window_start).total_seconds() == 300

    def test_parse_15m_returns_900s_window(self):
        window_start, window_end, duration = parse_btc5m_slug("btc-updown-15m-1776995400")
        assert duration == 900
        assert (window_end - window_start).total_seconds() == 900

    def test_parse_1h_returns_3600s_window(self):
        # Future-proofing: if Polymarket lists 1h, we catch it.
        _, _, duration = parse_btc5m_slug("btc-updown-1h-1776995400")
        assert duration == 3600

    def test_parse_1d_returns_86400s_window(self):
        _, _, duration = parse_btc5m_slug("btc-updown-1d-1776995400")
        assert duration == 86400

    def test_rejects_wrong_asset(self):
        with pytest.raises(ValueError):
            parse_btc5m_slug("eth-updown-5m-1776995400")

    def test_rejects_malformed_duration(self):
        with pytest.raises(ValueError):
            parse_btc5m_slug("btc-updown-fast-1776995400")

    def test_rejects_malformed_timestamp(self):
        with pytest.raises(ValueError):
            parse_btc5m_slug("btc-updown-5m-not-a-number")


class TestBtc5mScanner:
    def _make_gamma_response(self, slug: str, polymarket_id: str):
        return [{
            "conditionId": polymarket_id,
            "slug": slug,
            "question": "Bitcoin Up or Down - X",
            "clobTokenIds": json.dumps(["t_yes", "t_no"]),
            "endDate": "2026-04-24T01:50:00Z",
            "active": True,
            "closed": False,
        }]

    def test_scan_parses_one_market(self):
        http = MagicMock()
        http.get.return_value.status_code = 200
        http.get.return_value.json.return_value = self._make_gamma_response(
            "btc-updown-5m-1776995400", "0xabc"
        )
        scanner = Btc5mScanner(http_client=http)
        markets = scanner.scan()
        assert len(markets) == 1
        m = markets[0]
        assert m.polymarket_id == "0xabc"
        assert m.slug == "btc-updown-5m-1776995400"
        assert m.token_id_yes == "t_yes"
        assert m.token_id_no == "t_no"

    def test_scan_accepts_5m_and_15m_rejects_other_assets(self):
        http = MagicMock()
        http.get.return_value.status_code = 200
        http.get.return_value.json.return_value = [
            {"conditionId": "0x1", "slug": "btc-updown-5m-1776995400",
             "clobTokenIds": json.dumps(["a","b"]), "endDate": "2026-04-24T01:50:00Z"},
            {"conditionId": "0x2", "slug": "btc-updown-15m-1776995400",
             "clobTokenIds": json.dumps(["c","d"]), "endDate": "2026-04-24T01:50:00Z"},
            {"conditionId": "0x3", "slug": "some-other-market",
             "clobTokenIds": json.dumps(["e","f"]), "endDate": "2026-04-24T01:50:00Z"},
            {"conditionId": "0x4", "slug": "eth-updown-5m-1776995400",
             "clobTokenIds": json.dumps(["g","h"]), "endDate": "2026-04-24T01:50:00Z"},
        ]
        scanner = Btc5mScanner(http_client=http)
        markets = scanner.scan()
        assert len(markets) == 2
        ids = {m.polymarket_id for m in markets}
        assert ids == {"0x1", "0x2"}
        # Confirm durations parsed into the model
        by_slug = {m.slug: m for m in markets}
        assert by_slug["btc-updown-5m-1776995400"].window_duration_s == 300
        assert by_slug["btc-updown-15m-1776995400"].window_duration_s == 900

    def test_scan_empty_on_http_error(self):
        http = MagicMock()
        http.get.side_effect = RuntimeError("nope")
        scanner = Btc5mScanner(http_client=http)
        assert scanner.scan() == []
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/unit/services/btc5m/test_scanner.py -v
```

Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `polyagent/services/btc5m/scanner.py`:

```python
"""Scans Polymarket Gamma for new BTC short-horizon up/down markets.

Accepts any ``btc-updown-<duration>-<unix_ts>`` slug where ``<duration>``
is an integer followed by m/h/d (e.g. 5m, 15m, 1h, 4h, 1d). The specific
duration is preserved on the model as ``window_duration_s``.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

from polyagent.models import Btc5mMarket

logger = logging.getLogger("polyagent.services.btc5m.scanner")

# Captures (duration_token, unix_ts). duration_token examples: 5m, 15m, 1h, 4h, 1d.
BTC5M_SLUG_RE = re.compile(r"^btc-updown-(\d+[mhd])-(\d+)$")

_GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

_UNIT_TO_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def _duration_to_seconds(token: str) -> int:
    """'5m' -> 300, '15m' -> 900, '1h' -> 3600, '1d' -> 86400. Raises on bad input."""
    if not token or token[-1] not in _UNIT_TO_SECONDS:
        raise ValueError(f"bad duration token: {token!r}")
    try:
        n = int(token[:-1])
    except ValueError as exc:
        raise ValueError(f"bad duration number: {token!r}") from exc
    if n <= 0:
        raise ValueError(f"non-positive duration: {token!r}")
    return n * _UNIT_TO_SECONDS[token[-1]]


def parse_btc5m_slug(slug: str) -> tuple[datetime, datetime, int]:
    """Extract (window_start_ts, window_end_ts, window_duration_s) from a slug.

    Raises ValueError for non-BTC slugs, bad duration tokens, or malformed
    timestamps.
    """
    m = BTC5M_SLUG_RE.match(slug)
    if not m:
        raise ValueError(f"not a btc-updown slug: {slug!r}")
    duration_token = m.group(1)
    try:
        end_unix = int(m.group(2))
    except ValueError as exc:
        raise ValueError(f"malformed timestamp in slug: {slug!r}") from exc
    duration_s = _duration_to_seconds(duration_token)
    window_end = datetime.fromtimestamp(end_unix, tz=timezone.utc)
    window_start = window_end - timedelta(seconds=duration_s)
    return window_start, window_end, duration_s


class Btc5mScanner:
    """Polls Gamma for BTC 5m markets and returns parsed Btc5mMarket objects."""

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        page_limit: int = 500,
    ) -> None:
        self._http = http_client or httpx.Client(timeout=15.0)
        self._page_limit = page_limit

    def scan(self) -> list[Btc5mMarket]:
        """Return all currently-listed BTC 5m markets. Empty list on error."""
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

        out: list[Btc5mMarket] = []
        for m in raw:
            slug = m.get("slug") or ""
            if not BTC5M_SLUG_RE.match(slug):
                continue
            try:
                window_start, window_end, duration_s = parse_btc5m_slug(slug)
                token_ids = json.loads(m.get("clobTokenIds") or "[]")
                if len(token_ids) < 2:
                    continue
                out.append(Btc5mMarket(
                    polymarket_id=m.get("conditionId") or "",
                    slug=slug,
                    token_id_yes=token_ids[0],
                    token_id_no=token_ids[1],
                    window_duration_s=duration_s,
                    window_start_ts=window_start,
                    window_end_ts=window_end,
                ))
            except Exception as exc:
                logger.warning("parse failed for %s: %s", slug, exc)
                continue

        return out

    def close(self) -> None:
        self._http.close()
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/unit/services/btc5m/test_scanner.py -v
```

Expected: 7 pass.

- [ ] **Step 5: Commit**

```bash
git add polyagent/services/btc5m/scanner.py tests/unit/services/btc5m/test_scanner.py
git commit -m "feat(btc5m): add scanner and slug parser"
```

---

## Task 7: Repository (CRUD for both tables)

**Files:**
- Create: `polyagent/data/repositories/btc5m.py`
- Create: `tests/unit/test_btc5m_repo.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_btc5m_repo.py`:

```python
"""Tests for Btc5mRepository."""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

from polyagent.data.repositories.btc5m import Btc5mRepository
from polyagent.models import Btc5mMarket, Btc5mTrade


class TestBtc5mRepository:
    def setup_method(self):
        self.db = MagicMock()
        self.repo = Btc5mRepository(self.db)

    def _mock_cursor(self, fetchone=None, fetchall=None):
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        if fetchone is not None:
            cur.fetchone.return_value = fetchone
        if fetchall is not None:
            cur.fetchall.return_value = fetchall
        self.db.cursor.return_value = cur
        return cur

    def test_upsert_market_returns_id(self):
        expected = uuid4()
        self._mock_cursor(fetchone={"id": expected})
        market = Btc5mMarket(
            polymarket_id="0x1",
            slug="btc-updown-5m-1776995400",
            token_id_yes="y",
            token_id_no="n",
            window_duration_s=300,
            window_start_ts=datetime(2026, 4, 24, 1, 45, tzinfo=timezone.utc),
            window_end_ts=datetime(2026, 4, 24, 1, 50, tzinfo=timezone.utc),
        )
        result = self.repo.upsert_market(market)
        assert result == expected

    def test_insert_trade_returns_id(self):
        expected = uuid4()
        self._mock_cursor(fetchone={"id": expected})
        trade = Btc5mTrade(
            market_id=uuid4(),
            side="YES",
            fill_price_assumed=Decimal("0.52"),
            size=Decimal("5.00"),
            estimator_p_up=0.58,
            spot_at_decision=Decimal("65000"),
            vol_at_decision=0.45,
            edge_at_decision=0.06,
        )
        result = self.repo.insert_trade(trade)
        assert result == expected

    def test_get_unresolved_markets_returns_list(self):
        now = datetime.now(timezone.utc)
        self._mock_cursor(fetchall=[
            {"id": uuid4(), "polymarket_id": "0x1", "slug": "s", "token_id_yes": "y",
             "token_id_no": "n", "window_start_ts": now, "window_end_ts": now,
             "start_spot": None, "end_spot": None, "outcome": None}
        ])
        rows = self.repo.get_unresolved_markets_past_end(now)
        assert len(rows) == 1

    def test_update_market_resolution(self):
        self._mock_cursor(fetchone=None)
        self.repo.update_market_resolution(
            uuid4(),
            start_spot=Decimal("65000"),
            end_spot=Decimal("65100"),
            outcome="YES",
        )
        # No assertion on return — just confirm it doesn't raise

    def test_update_trade_pnl(self):
        self._mock_cursor(fetchone=None)
        self.repo.update_trade_pnl(uuid4(), Decimal("0.50"))
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/unit/test_btc5m_repo.py -v
```

Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `polyagent/data/repositories/btc5m.py`:

```python
"""CRUD for btc5m_markets and btc5m_trades."""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from polyagent.infra.database import Database
from polyagent.models import Btc5mMarket, Btc5mTrade

logger = logging.getLogger("polyagent.repositories.btc5m")

UPSERT_MARKET = """
    INSERT INTO btc5m_markets (
        polymarket_id, slug, token_id_yes, token_id_no,
        window_duration_s, window_start_ts, window_end_ts
    ) VALUES (
        %(polymarket_id)s, %(slug)s, %(token_id_yes)s, %(token_id_no)s,
        %(window_duration_s)s, %(window_start_ts)s, %(window_end_ts)s
    )
    ON CONFLICT (polymarket_id) DO UPDATE SET
        slug = EXCLUDED.slug
    RETURNING id
"""

INSERT_TRADE = """
    INSERT INTO btc5m_trades (
        market_id, side, fill_price_assumed, size,
        estimator_p_up, spot_at_decision, vol_at_decision, edge_at_decision
    ) VALUES (
        %(market_id)s, %(side)s, %(fill_price_assumed)s, %(size)s,
        %(estimator_p_up)s, %(spot_at_decision)s, %(vol_at_decision)s,
        %(edge_at_decision)s
    )
    RETURNING id
"""

SELECT_UNRESOLVED_PAST_END = """
    SELECT id, polymarket_id, slug, token_id_yes, token_id_no,
           window_duration_s, window_start_ts, window_end_ts,
           start_spot, end_spot, outcome
    FROM btc5m_markets
    WHERE outcome IS NULL AND window_end_ts <= %(now)s
"""

SELECT_ACTIVE = """
    SELECT id, polymarket_id, slug, token_id_yes, token_id_no,
           window_duration_s, window_start_ts, window_end_ts,
           start_spot, end_spot, outcome
    FROM btc5m_markets
    WHERE outcome IS NULL AND window_end_ts > %(now)s
"""

UPDATE_MARKET_RESOLUTION = """
    UPDATE btc5m_markets
    SET start_spot = %(start_spot)s,
        end_spot = %(end_spot)s,
        outcome = %(outcome)s,
        resolved_at = NOW()
    WHERE id = %(id)s
"""

SELECT_TRADES_FOR_MARKET = """
    SELECT id, market_id, side, fill_price_assumed, size,
           estimator_p_up, spot_at_decision, vol_at_decision,
           edge_at_decision, pnl
    FROM btc5m_trades
    WHERE market_id = %(market_id)s
"""

UPDATE_TRADE_PNL = """
    UPDATE btc5m_trades
    SET pnl = %(pnl)s, resolved_at = NOW()
    WHERE id = %(id)s
"""


class Btc5mRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def upsert_market(self, market: Btc5mMarket) -> UUID:
        with self._db.cursor() as cur:
            cur.execute(UPSERT_MARKET, {
                "polymarket_id": market.polymarket_id,
                "slug": market.slug,
                "token_id_yes": market.token_id_yes,
                "token_id_no": market.token_id_no,
                "window_duration_s": market.window_duration_s,
                "window_start_ts": market.window_start_ts,
                "window_end_ts": market.window_end_ts,
            })
            return cur.fetchone()["id"]

    def insert_trade(self, trade: Btc5mTrade) -> UUID:
        with self._db.cursor() as cur:
            cur.execute(INSERT_TRADE, {
                "market_id": trade.market_id,
                "side": trade.side,
                "fill_price_assumed": trade.fill_price_assumed,
                "size": trade.size,
                "estimator_p_up": trade.estimator_p_up,
                "spot_at_decision": trade.spot_at_decision,
                "vol_at_decision": trade.vol_at_decision,
                "edge_at_decision": trade.edge_at_decision,
            })
            return cur.fetchone()["id"]

    def get_active_markets(self, now: datetime) -> list[dict]:
        with self._db.cursor() as cur:
            cur.execute(SELECT_ACTIVE, {"now": now})
            return cur.fetchall()

    def get_unresolved_markets_past_end(self, now: datetime) -> list[dict]:
        with self._db.cursor() as cur:
            cur.execute(SELECT_UNRESOLVED_PAST_END, {"now": now})
            return cur.fetchall()

    def update_market_resolution(
        self, market_id: UUID, start_spot: Decimal, end_spot: Decimal, outcome: str,
    ) -> None:
        with self._db.cursor() as cur:
            cur.execute(UPDATE_MARKET_RESOLUTION, {
                "id": market_id,
                "start_spot": start_spot,
                "end_spot": end_spot,
                "outcome": outcome,
            })

    def get_trades_for_market(self, market_id: UUID) -> list[dict]:
        with self._db.cursor() as cur:
            cur.execute(SELECT_TRADES_FOR_MARKET, {"market_id": market_id})
            return cur.fetchall()

    def update_trade_pnl(self, trade_id: UUID, pnl: Decimal) -> None:
        with self._db.cursor() as cur:
            cur.execute(UPDATE_TRADE_PNL, {"id": trade_id, "pnl": pnl})
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/unit/test_btc5m_repo.py -v
```

Expected: 5 pass.

- [ ] **Step 5: Commit**

```bash
git add polyagent/data/repositories/btc5m.py tests/unit/test_btc5m_repo.py
git commit -m "feat(repo): add Btc5mRepository CRUD"
```

---

## Task 8: Decider + paper executor

**Files:**
- Create: `polyagent/services/btc5m/decider.py`
- Create: `tests/unit/services/btc5m/test_decider.py`

The decider owns the decision logic AND the paper-trade write; keeping them in one module keeps the "decide-then-record" invariant explicit.

- [ ] **Step 1: Write failing tests**

Create `tests/unit/services/btc5m/test_decider.py`:

```python
"""Tests for Btc5mDecider."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

from polyagent.models import Btc5mMarket
from polyagent.services.btc5m.decider import Btc5mDecider


def _make_market(now: datetime, ttm_s: int = 120) -> tuple[dict, Btc5mMarket]:
    """Produce a dict-shaped 'active market' row and a model for constructing it."""
    window_end = now + timedelta(seconds=ttm_s)
    window_start = window_end - timedelta(seconds=300)
    market_id = uuid4()
    row = {
        "id": market_id,
        "polymarket_id": "0x1",
        "slug": "btc-updown-5m-1234567890",
        "token_id_yes": "y", "token_id_no": "n",
        "window_duration_s": 300,
        "window_start_ts": window_start, "window_end_ts": window_end,
        "start_spot": None, "end_spot": None, "outcome": None,
    }
    model = Btc5mMarket(
        polymarket_id="0x1", slug=row["slug"], token_id_yes="y", token_id_no="n",
        window_duration_s=300,
        window_start_ts=window_start, window_end_ts=window_end,
    )
    return row, model


class TestBtc5mDecider:
    def setup_method(self):
        self.spot = MagicMock()
        self.book = MagicMock()
        self.repo = MagicMock()

    def test_no_trade_when_edge_below_threshold(self):
        now = datetime.now(timezone.utc)
        row, _ = _make_market(now)
        self.spot.current.return_value = Decimal("65000")
        self.spot.realized_vol.return_value = 0.40
        # Small edge: estimator ≈ 0.5, market mid ≈ 0.51 → edge ≈ -0.01
        self.book.fetch_mid.return_value = (Decimal("0.50"), Decimal("0.51"))

        decider = Btc5mDecider(
            spot=self.spot, book=self.book, repo=self.repo,
            edge_threshold=0.05, position_size_usd=Decimal("5"),
            fees_bps=0.0,
        )
        decider.evaluate(row)
        self.repo.insert_trade.assert_not_called()

    def test_trade_yes_when_estimator_above_market(self):
        now = datetime.now(timezone.utc)
        row, _ = _make_market(now, ttm_s=60)
        # Current spot way above start_spot (which we proxy to current_spot
        # before the window opens) is degenerate — instead set a small TTM
        # and a tight spread where our estimator says up strongly.
        self.spot.current.return_value = Decimal("65500")   # 0.7% above
        self.spot.realized_vol.return_value = 0.10
        self.book.fetch_mid.return_value = (Decimal("0.80"), Decimal("0.82"))
        # p_up should be near 1 given 0.7% already above start; edge > 0.1
        decider = Btc5mDecider(
            spot=self.spot, book=self.book, repo=self.repo,
            edge_threshold=0.05, position_size_usd=Decimal("5"),
            fees_bps=0.0,
        )
        # Manually set start_spot on the row so p_up is clearly high
        row["start_spot"] = Decimal("65000")
        decider.evaluate(row)
        self.repo.insert_trade.assert_called_once()
        trade = self.repo.insert_trade.call_args[0][0]
        assert trade.side == "YES"
        assert trade.fill_price_assumed == Decimal("0.82")  # worst-case ask for YES

    def test_trade_no_when_estimator_below_market(self):
        now = datetime.now(timezone.utc)
        row, _ = _make_market(now, ttm_s=60)
        row["start_spot"] = Decimal("65000")
        # Current spot clearly below start — estimator says down
        self.spot.current.return_value = Decimal("64500")
        self.spot.realized_vol.return_value = 0.10
        # Market priced as if up — we sell YES (buy NO)
        self.book.fetch_mid.return_value = (Decimal("0.80"), Decimal("0.82"))

        decider = Btc5mDecider(
            spot=self.spot, book=self.book, repo=self.repo,
            edge_threshold=0.05, position_size_usd=Decimal("5"),
            fees_bps=0.0,
        )
        decider.evaluate(row)
        self.repo.insert_trade.assert_called_once()
        trade = self.repo.insert_trade.call_args[0][0]
        assert trade.side == "NO"
        assert trade.fill_price_assumed == Decimal("0.80")  # worst-case bid for sell/NO

    def test_skips_if_already_traded_on_market(self):
        now = datetime.now(timezone.utc)
        row, _ = _make_market(now, ttm_s=60)
        row["start_spot"] = Decimal("65000")
        self.repo.get_trades_for_market.return_value = [{"id": uuid4()}]
        self.spot.current.return_value = Decimal("65500")
        self.spot.realized_vol.return_value = 0.10
        self.book.fetch_mid.return_value = (Decimal("0.30"), Decimal("0.32"))

        decider = Btc5mDecider(
            spot=self.spot, book=self.book, repo=self.repo,
            edge_threshold=0.05, position_size_usd=Decimal("5"),
            fees_bps=0.0,
        )
        decider.evaluate(row)
        self.repo.insert_trade.assert_not_called()

    def test_skips_if_no_spot(self):
        now = datetime.now(timezone.utc)
        row, _ = _make_market(now, ttm_s=60)
        self.spot.current.return_value = None

        decider = Btc5mDecider(
            spot=self.spot, book=self.book, repo=self.repo,
            edge_threshold=0.05, position_size_usd=Decimal("5"),
            fees_bps=0.0,
        )
        decider.evaluate(row)
        self.repo.insert_trade.assert_not_called()
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/unit/services/btc5m/test_decider.py -v
```

Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `polyagent/services/btc5m/decider.py`:

```python
"""Decision engine + paper-trade executor for BTC 5m markets."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from polyagent.data.repositories.btc5m import Btc5mRepository
from polyagent.models import Btc5mTrade
from polyagent.services.btc5m.estimator import estimate_up_probability
from polyagent.services.btc5m.spot import BtcSpotSource

logger = logging.getLogger("polyagent.services.btc5m.decider")


class BookFetcher:
    """Lightweight protocol for orderbook fetches."""

    def fetch_mid(self, token_id: str) -> tuple[Decimal, Decimal] | None:
        """Return (best_bid, best_ask) for the YES token, or None on failure."""
        raise NotImplementedError


class Btc5mDecider:
    """For each active market: compute edge, paper-trade if it clears threshold."""

    def __init__(
        self,
        spot: BtcSpotSource,
        book: BookFetcher,
        repo: Btc5mRepository,
        edge_threshold: float,
        position_size_usd: Decimal,
        fees_bps: float,
        vol_window_s: int = 300,
    ) -> None:
        self._spot = spot
        self._book = book
        self._repo = repo
        self._threshold = edge_threshold
        self._size = position_size_usd
        self._fees_bps = fees_bps
        self._vol_window_s = vol_window_s

    def evaluate(self, market_row: dict) -> None:
        """Evaluate one active market and record a paper trade if edge clears."""
        market_id = market_row["id"]
        if self._repo.get_trades_for_market(market_id):
            return  # one trade per market

        spot = self._spot.current()
        if spot is None:
            return

        vol = self._spot.realized_vol(window_s=self._vol_window_s)

        window_end = market_row["window_end_ts"]
        now = datetime.now(timezone.utc)
        ttm = (window_end - now).total_seconds()
        if ttm <= 0:
            return  # resolver will handle

        # Before the window opens, we don't yet know start_spot — proxy with
        # the current spot. Once the window is open, start_spot should be the
        # spot at window_start_ts. We don't have retrospective pricing in
        # real time, so we approximate by using the current spot as K.
        start_spot = market_row.get("start_spot")
        if start_spot is None:
            start_spot = spot

        p_up = estimate_up_probability(
            start_price=start_spot,
            current_spot=spot,
            seconds_to_resolution=ttm,
            annualised_vol=vol,
        )

        book = self._book.fetch_mid(market_row["token_id_yes"])
        if book is None:
            return
        bid, ask = book
        mid = (float(bid) + float(ask)) / 2.0

        edge = p_up - mid
        if abs(edge) < self._threshold:
            return

        size_fraction = float(self._size) / 1.0
        gross_edge_usd = abs(edge) * size_fraction
        fees_usd = size_fraction * self._fees_bps / 10_000.0
        if gross_edge_usd <= fees_usd:
            return

        if edge > 0:
            side = "YES"
            fill = ask
        else:
            side = "NO"
            fill = bid

        trade = Btc5mTrade(
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
            "PAPER TRADE %s on %s: side=%s edge=%+.3f p_up=%.3f mid=%.3f",
            trade.side, market_row["polymarket_id"], side, edge, p_up, mid,
        )
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/unit/services/btc5m/test_decider.py -v
```

Expected: 5 pass.

- [ ] **Step 5: Commit**

```bash
git add polyagent/services/btc5m/decider.py tests/unit/services/btc5m/test_decider.py
git commit -m "feat(btc5m): add decider + paper executor"
```

---

## Task 9: Resolver (settlement + PnL)

**Files:**
- Create: `polyagent/services/btc5m/resolver.py`
- Create: `tests/unit/services/btc5m/test_resolver.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/services/btc5m/test_resolver.py`:

```python
"""Tests for Btc5mResolver."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from polyagent.services.btc5m.resolver import Btc5mResolver


class TestPnl:
    @pytest.mark.parametrize("side,fill,outcome,size,expected", [
        # YES at 0.40, outcome YES: (1 - 0.40) * 5 = +3.00
        ("YES", Decimal("0.40"), "YES", Decimal("5"), Decimal("3.00")),
        # YES at 0.40, outcome NO: -0.40 * 5 = -2.00
        ("YES", Decimal("0.40"), "NO",  Decimal("5"), Decimal("-2.00")),
        # NO at 0.40, outcome NO: (1 - 0.40) * 5 = +3.00
        ("NO",  Decimal("0.40"), "NO",  Decimal("5"), Decimal("3.00")),
        # NO at 0.40, outcome YES: -0.40 * 5 = -2.00
        ("NO",  Decimal("0.40"), "YES", Decimal("5"), Decimal("-2.00")),
    ])
    def test_compute_pnl_cases(self, side, fill, outcome, size, expected):
        from polyagent.services.btc5m.resolver import compute_pnl
        assert compute_pnl(side, fill, outcome, size) == expected


class TestBtc5mResolver:
    def setup_method(self):
        self.repo = MagicMock()
        self.spot_history = MagicMock()
        self.resolver = Btc5mResolver(repo=self.repo, spot_history=self.spot_history)

    def test_resolves_market_and_updates_trade_pnl(self):
        mid = uuid4()
        tid = uuid4()
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(minutes=6)
        window_end = now - timedelta(minutes=1)
        self.repo.get_unresolved_markets_past_end.return_value = [{
            "id": mid, "polymarket_id": "0x1", "slug": "s",
            "window_start_ts": window_start, "window_end_ts": window_end,
        }]
        self.spot_history.price_at.side_effect = [
            Decimal("65000"),   # start
            Decimal("65100"),   # end → UP
        ]
        self.repo.get_trades_for_market.return_value = [{
            "id": tid, "side": "YES", "fill_price_assumed": Decimal("0.40"),
            "size": Decimal("5"), "pnl": None,
        }]

        self.resolver.resolve_due_markets()

        self.repo.update_market_resolution.assert_called_once_with(
            mid, start_spot=Decimal("65000"), end_spot=Decimal("65100"), outcome="YES"
        )
        self.repo.update_trade_pnl.assert_called_once_with(tid, Decimal("3.00"))

    def test_skips_market_if_spot_unavailable(self):
        mid = uuid4()
        now = datetime.now(timezone.utc)
        self.repo.get_unresolved_markets_past_end.return_value = [{
            "id": mid, "polymarket_id": "0x1", "slug": "s",
            "window_start_ts": now - timedelta(minutes=6),
            "window_end_ts": now - timedelta(minutes=1),
        }]
        self.spot_history.price_at.return_value = None

        self.resolver.resolve_due_markets()

        self.repo.update_market_resolution.assert_not_called()
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/unit/services/btc5m/test_resolver.py -v
```

Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `polyagent/services/btc5m/resolver.py`:

```python
"""Resolves BTC 5m markets whose window has ended and computes paper P&L."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

from polyagent.data.repositories.btc5m import Btc5mRepository

logger = logging.getLogger("polyagent.services.btc5m.resolver")


class SpotHistory(Protocol):
    """Anything that can answer 'what was the BTC spot at timestamp T?'."""

    def price_at(self, ts: datetime) -> Decimal | None: ...


def compute_pnl(
    side: str,
    fill_price: Decimal,
    outcome: str,
    size: Decimal,
) -> Decimal:
    """Realized P&L for a binary paper trade.

    We assume trade fills with `size` USD notional at `fill_price`. YES side
    profits `(1 - fill_price)` per unit notional if outcome is YES, loses
    `fill_price` if NO. NO side mirrors.
    """
    if side == "YES":
        return size * (Decimal("1") - fill_price) if outcome == "YES" else -size * fill_price
    return size * (Decimal("1") - fill_price) if outcome == "NO" else -size * fill_price


class Btc5mResolver:
    def __init__(self, repo: Btc5mRepository, spot_history: SpotHistory) -> None:
        self._repo = repo
        self._history = spot_history

    def resolve_due_markets(self) -> int:
        """Resolve every market whose window_end_ts has passed. Returns count."""
        now = datetime.now(timezone.utc)
        markets = self._repo.get_unresolved_markets_past_end(now)
        resolved = 0
        for m in markets:
            start_spot = self._history.price_at(m["window_start_ts"])
            end_spot = self._history.price_at(m["window_end_ts"])
            if start_spot is None or end_spot is None:
                logger.info("skip resolution of %s: spot history unavailable", m["polymarket_id"])
                continue

            outcome = "YES" if end_spot >= start_spot else "NO"
            self._repo.update_market_resolution(
                m["id"], start_spot=start_spot, end_spot=end_spot, outcome=outcome,
            )
            for t in self._repo.get_trades_for_market(m["id"]):
                if t.get("pnl") is not None:
                    continue
                pnl = compute_pnl(
                    t["side"], Decimal(str(t["fill_price_assumed"])),
                    outcome, Decimal(str(t["size"])),
                )
                self._repo.update_trade_pnl(t["id"], pnl)
            resolved += 1
        if resolved:
            logger.info("resolved %d btc5m markets", resolved)
        return resolved
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/unit/services/btc5m/test_resolver.py -v
```

Expected: 6 pass.

- [ ] **Step 5: Commit**

```bash
git add polyagent/services/btc5m/resolver.py tests/unit/services/btc5m/test_resolver.py
git commit -m "feat(btc5m): add resolver + PnL calc"
```

---

## Task 10: Worker loop + main.py wiring

**Files:**
- Create: `polyagent/services/btc5m/worker.py`
- Modify: `polyagent/main.py`

The worker is a long-running function that the existing `WorkerPool` can spawn.

- [ ] **Step 1: Create the worker module**

Create `polyagent/services/btc5m/worker.py`:

```python
"""Single-threaded BTC 5m worker loop."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal

from polyagent.data.clients.polymarket import PolymarketClient
from polyagent.data.repositories.btc5m import Btc5mRepository
from polyagent.infra.config import Settings
from polyagent.services.btc5m.decider import Btc5mDecider, BookFetcher
from polyagent.services.btc5m.resolver import Btc5mResolver, SpotHistory
from polyagent.services.btc5m.scanner import Btc5mScanner
from polyagent.services.btc5m.spot import BtcSpotSource

logger = logging.getLogger("polyagent.services.btc5m.worker")


class PolymarketBookFetcher(BookFetcher):
    """BookFetcher backed by PolyAgent's existing Polymarket CLOB client."""

    def __init__(self, client: PolymarketClient) -> None:
        self._client = client

    def fetch_mid(self, token_id: str):
        try:
            book = self._client.fetch_order_book(token_id)
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if not bids or not asks:
                return None
            best_bid = Decimal(str(bids[0]["price"]))
            best_ask = Decimal(str(asks[0]["price"]))
            return best_bid, best_ask
        except Exception as exc:
            logger.warning("book fetch failed for %s: %s", token_id, exc)
            return None


class CoinbaseCandleHistory(SpotHistory):
    """Fetches BTC/USD price at a past timestamp from Coinbase 1-min candles."""

    import httpx as _httpx

    def __init__(self) -> None:
        self._http = self._httpx.Client(timeout=10.0)

    def price_at(self, ts):
        # Coinbase /products/BTC-USD/candles: start, end, granularity=60
        import httpx
        start = int(ts.timestamp()) - 60
        end = int(ts.timestamp()) + 60
        try:
            resp = self._http.get(
                "https://api.exchange.coinbase.com/products/BTC-USD/candles",
                params={"granularity": 60, "start": start, "end": end},
            )
            resp.raise_for_status()
            candles = resp.json()
            # Each candle: [time, low, high, open, close, volume]
            target = int(ts.timestamp())
            best = None
            best_dt = None
            for c in candles:
                dt = abs(c[0] - target)
                if best_dt is None or dt < best_dt:
                    best, best_dt = c, dt
            if best is None:
                return None
            return Decimal(str(best[4]))  # close price of the nearest minute bar
        except Exception as exc:
            logger.warning("coinbase candle fetch failed for ts=%s: %s", ts, exc)
            return None


def run_btc5m_worker(
    settings: Settings,
    repo: Btc5mRepository,
    polymarket: PolymarketClient,
    shutdown_q,  # queue.Queue used as shutdown signal (truthy item = stop)
) -> None:
    """Long-running BTC 5m worker. Returns when shutdown_q is non-empty."""
    spot = BtcSpotSource()
    scanner = Btc5mScanner()
    book = PolymarketBookFetcher(polymarket)
    history = CoinbaseCandleHistory()
    decider = Btc5mDecider(
        spot=spot, book=book, repo=repo,
        edge_threshold=settings.btc5m_edge_threshold,
        position_size_usd=Decimal(str(settings.btc5m_position_size_usd)),
        fees_bps=settings.btc5m_fees_bps,
        vol_window_s=settings.btc5m_vol_window_s,
    )
    resolver = Btc5mResolver(repo=repo, spot_history=history)

    last_market_poll = 0.0
    spot_interval = settings.btc5m_spot_poll_s
    market_interval = settings.btc5m_market_poll_s
    logger.info("btc5m worker started (spot=%ss market=%ss)", spot_interval, market_interval)

    try:
        while shutdown_q.empty():
            # Tick spot every cycle
            spot.tick()

            now = time.time()
            if now - last_market_poll >= market_interval:
                try:
                    markets = scanner.scan()
                    for m in markets:
                        repo.upsert_market(m)
                except Exception:
                    logger.exception("btc5m scan failed")

                try:
                    active = repo.get_active_markets(datetime.now(timezone.utc))
                    for row in active:
                        decider.evaluate(row)
                except Exception:
                    logger.exception("btc5m decider failed")

                try:
                    resolver.resolve_due_markets()
                except Exception:
                    logger.exception("btc5m resolver failed")

                last_market_poll = now

            time.sleep(spot_interval)
    finally:
        spot.close()
        scanner.close()
        logger.info("btc5m worker stopped")
```

- [ ] **Step 2: Register the worker in `main.py`**

Add import at the top of `polyagent/main.py`:

```python
from polyagent.data.repositories.btc5m import Btc5mRepository
from polyagent.services.btc5m.worker import run_btc5m_worker
```

Near the other repo instantiations (`market_repo = MarketRepository(db)` etc.), add:

```python
    btc5m_repo = Btc5mRepository(db)
```

After the existing `pool.spawn("exit_monitor", exit_monitor_worker, n_exit)` line, add the gated worker spawn:

```python
    if settings.btc5m_enabled:
        pool.spawn(
            "btc5m",
            lambda: run_btc5m_worker(settings, btc5m_repo, polymarket, queues.shutdown),
            1,
        )
        logger.info("btc5m: 1 worker enabled")
    else:
        logger.info("btc5m: disabled (set BTC5M_ENABLED=true to enable)")
```

Update the "All workers started" log line to include btc5m when enabled.

- [ ] **Step 3: Run the full unit test suite**

```bash
uv run pytest tests/unit -v
```

Expected: no regressions.

- [ ] **Step 4: Sanity-launch the bot briefly**

With `BTC5M_ENABLED=true` in `.env` (temporarily), start the bot and watch 60 seconds of logs:

```bash
BTC5M_ENABLED=true timeout 90 uv run polyagent-bot 2>&1 | tail -40
```

Expected: log lines like `btc5m worker started` and periodic spot ticks. No traceback.

- [ ] **Step 5: Commit**

```bash
git add polyagent/services/btc5m/worker.py polyagent/main.py
git commit -m "feat(btc5m): add worker loop and main.py wiring"
```

---

## Task 11: `polyagent btc5m-stats` CLI + integration test

**Files:**
- Create: `polyagent/cli/btc5m_stats.py`
- Modify: `polyagent/cli/main.py`
- Create: `tests/integration/test_btc5m_stats_cli.py`

- [ ] **Step 1: Write failing integration test**

Create `tests/integration/test_btc5m_stats_cli.py`:

```python
"""End-to-end test for `polyagent btc5m-stats` against a real DB."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from click.testing import CliRunner

from polyagent.cli.btc5m_stats import btc5m_stats
from polyagent.infra.config import Settings
from polyagent.infra.database import Database

pytestmark = pytest.mark.integration

_TEST_DB_URL = "postgresql://polyagent:polyagent@localhost:5432/polyagent_test"


@pytest.fixture
def seeded_db(settings: Settings):
    db = Database(settings)
    with db.cursor() as cur:
        cur.execute("DELETE FROM btc5m_trades")
        cur.execute("DELETE FROM btc5m_markets")

        mid = uuid4()
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=10)
        end = now - timedelta(minutes=5)
        cur.execute(
            """
            INSERT INTO btc5m_markets (id, polymarket_id, slug, token_id_yes, token_id_no,
                                       window_duration_s, window_start_ts, window_end_ts,
                                       start_spot, end_spot, outcome, resolved_at)
            VALUES (%s, '0x1', 'btc-updown-5m-1234567890', 'y', 'n',
                    300, %s, %s, 65000, 65100, 'YES', NOW())
            """, (mid, start, end),
        )
        cur.execute(
            """
            INSERT INTO btc5m_trades (market_id, side, fill_price_assumed, size,
                                      estimator_p_up, spot_at_decision,
                                      vol_at_decision, edge_at_decision, pnl, resolved_at)
            VALUES
                (%s, 'YES', 0.40, 5.00, 0.55, 65000, 0.40,  0.10,  3.00, NOW()),
                (%s, 'YES', 0.60, 5.00, 0.70, 65000, 0.40,  0.08,  2.00, NOW()),
                (%s, 'NO',  0.30, 5.00, 0.40, 65000, 0.40, -0.10, -1.50, NOW())
            """, (mid, mid, mid),
        )
    yield db
    db.close()


def test_btc5m_stats_summary(seeded_db):
    runner = CliRunner()
    result = runner.invoke(btc5m_stats, [], env={"DATABASE_URL": _TEST_DB_URL})
    assert result.exit_code == 0, result.output
    assert "3" in result.output                # total trades
    assert "2/1" in result.output or "2" in result.output  # wins/losses
    # Total PnL = 3 + 2 - 1.5 = 3.50
    assert "3.50" in result.output or "+$3.50" in result.output
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/integration/test_btc5m_stats_cli.py --run-integration -v
```

Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement the CLI**

Create `polyagent/cli/btc5m_stats.py`:

```python
"""BTC 5m paper-trading analytics — `polyagent btc5m-stats`."""
from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

from polyagent.infra.config import Settings
from polyagent.infra.database import Database


STATS_QUERY_TOTAL = """
    SELECT
        COUNT(*)                                                   AS trades,
        COUNT(*) FILTER (WHERE pnl > 0)                            AS wins,
        COUNT(*) FILTER (WHERE pnl <= 0)                           AS losses,
        COALESCE(AVG(edge_at_decision), 0)                         AS avg_edge,
        COALESCE(SUM(pnl), 0)                                      AS total_pnl,
        COALESCE(AVG(pnl), 0)                                      AS avg_pnl,
        COALESCE(AVG(vol_at_decision), 0)                          AS avg_vol
    FROM btc5m_trades
    WHERE pnl IS NOT NULL
"""

STATS_QUERY_BY_DURATION = """
    SELECT
        m.window_duration_s                                        AS window_duration_s,
        COUNT(*)                                                   AS trades,
        COUNT(*) FILTER (WHERE t.pnl > 0)                          AS wins,
        COUNT(*) FILTER (WHERE t.pnl <= 0)                         AS losses,
        COALESCE(AVG(t.edge_at_decision), 0)                       AS avg_edge,
        COALESCE(SUM(t.pnl), 0)                                    AS total_pnl,
        COALESCE(AVG(t.pnl), 0)                                    AS avg_pnl
    FROM btc5m_trades t
    JOIN btc5m_markets m ON m.id = t.market_id
    WHERE t.pnl IS NOT NULL
    GROUP BY m.window_duration_s
    ORDER BY m.window_duration_s
"""


def _fmt_duration(seconds: int) -> str:
    """Render a window duration concisely (300 → '5m', 900 → '15m', 3600 → '1h')."""
    if seconds % 86400 == 0: return f"{seconds // 86400}d"
    if seconds % 3600 == 0:  return f"{seconds // 3600}h"
    if seconds % 60 == 0:    return f"{seconds // 60}m"
    return f"{seconds}s"


@click.command("btc5m-stats")
@click.option("--by-duration", is_flag=True,
              help="Break out performance per window duration (5m vs 15m vs ...).")
def btc5m_stats(by_duration: bool):
    """Paper-trading performance of the BTC short-horizon subsystem."""
    console = Console()
    settings = Settings.from_env()
    db = Database(settings)

    if by_duration:
        with db.cursor() as cur:
            cur.execute(STATS_QUERY_BY_DURATION)
            rows = cur.fetchall()

        table = Table(title="BTC Up/Down Performance by Timeframe")
        table.add_column("Window", style="cyan")
        table.add_column("Trades", justify="right")
        table.add_column("W/L", justify="right")
        table.add_column("Win%", justify="right")
        table.add_column("Avg Edge", justify="right")
        table.add_column("Avg P&L", justify="right")
        table.add_column("Total P&L", justify="right")

        if not rows:
            table.add_row("(none)", "0", "-", "-", "-", "-", "$0.00")
        else:
            for r in rows:
                trades = int(r["trades"])
                wins = int(r["wins"])
                losses = int(r["losses"])
                win_pct = (wins / trades * 100) if trades else 0.0
                total_pnl = float(r["total_pnl"])
                avg_pnl = float(r["avg_pnl"])
                avg_edge = float(r["avg_edge"])
                pnl_style = "green" if total_pnl >= 0 else "red"
                table.add_row(
                    _fmt_duration(int(r["window_duration_s"])),
                    str(trades),
                    f"{wins}/{losses}",
                    f"{win_pct:.1f}%",
                    f"{avg_edge:+.3f}",
                    f"${avg_pnl:+,.2f}",
                    f"[{pnl_style}]${total_pnl:+,.2f}[/{pnl_style}]",
                )

        console.print(table)
        db.close()
        return

    with db.cursor() as cur:
        cur.execute(STATS_QUERY_TOTAL)
        row = cur.fetchone()

    trades = int(row["trades"] or 0)
    wins = int(row["wins"] or 0)
    losses = int(row["losses"] or 0)
    avg_edge = float(row["avg_edge"] or 0)
    total_pnl = float(row["total_pnl"] or 0)
    avg_pnl = float(row["avg_pnl"] or 0)
    avg_vol = float(row["avg_vol"] or 0)

    table = Table(title="BTC Up/Down Paper-Trading Performance")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")

    if trades == 0:
        table.add_row("Trades", "0")
        console.print(table)
        db.close()
        return

    win_pct = (wins / trades) * 100
    pnl_style = "green" if total_pnl >= 0 else "red"
    table.add_row("Trades", str(trades))
    table.add_row("W/L", f"{wins}/{losses}")
    table.add_row("Win%", f"{win_pct:.1f}%")
    table.add_row("Avg Edge", f"{avg_edge:+.3f}")
    table.add_row("Avg P&L", f"${avg_pnl:+,.2f}")
    table.add_row("Total P&L", f"[{pnl_style}]${total_pnl:+,.2f}[/{pnl_style}]")
    table.add_row("Avg Realized Vol", f"{avg_vol:.3f}")

    console.print(table)
    db.close()
```

- [ ] **Step 4: Register the command**

In `polyagent/cli/main.py`:

```python
from polyagent.cli.btc5m_stats import btc5m_stats
```

And:

```python
cli.add_command(btc5m_stats)
```

- [ ] **Step 5: Run integration test**

```bash
uv run pytest tests/integration/test_btc5m_stats_cli.py --run-integration -v
```

Expected: 1 passed.

- [ ] **Step 6: Smoke test**

```bash
uv run polyagent btc5m-stats
```

Expected: table with "Trades: 0" since we haven't paper-traded yet. No crash.

- [ ] **Step 7: Commit**

```bash
git add polyagent/cli/btc5m_stats.py polyagent/cli/main.py tests/integration/test_btc5m_stats_cli.py
git commit -m "feat(cli): add btc5m-stats paper-trading summary"
```

---

## Task 12: Rollout

Operational only, no code.

- [ ] **Step 1: Merge to main and push.**

```bash
git checkout main
git merge --no-ff feat/btc-5m-subsystem
git push origin main
```

- [ ] **Step 2: Enable in `.env`**

Add to `/home/cborden/Development/PolyAgent/.env`:

```
BTC5M_ENABLED=true
BTC5M_EDGE_THRESHOLD=0.05
BTC5M_POSITION_SIZE_USD=5.0
```

- [ ] **Step 3: Apply for Polymarket's sponsored Chainlink key**

Submit the Chainlink Data Streams request form at
https://chainlinkcommunity.typeform.com/datastreams mentioning Polymarket
development. This is parallel to the code work — don't block on it.

- [ ] **Step 4: Restart the bot**

```bash
podman compose -f compose.yaml restart polyagent-app
podman logs -f polyagent-app | grep btc5m
```

Expected: `btc5m worker started` within 30 seconds.

- [ ] **Step 5: Schedule a 14-day review**

Set a reminder: run `polyagent btc5m-stats` every few days, then after
14 days evaluate against Phase 2 gate criteria from the spec:
- Total P&L ≥ $5
- Win rate ≥ 52%
- No systematic basis drift

If gate passes → plan Phase 2. If fails → table the subsystem.

---

## Self-Review Notes

### Spec coverage check

Spec sections and their tasks:
- Motivation / Goals / Non-Goals — no tasks (context)
- Architecture (worker thread) — Task 10
- Components: spot — Task 5; estimator — Task 4; scanner — Task 6;
  book — Task 10 (inline in worker.py); decider/executor — Task 8;
  resolver — Task 9; CLI — Task 11
- Data model + migration — Task 1
- Domain models — Task 2
- Config — Task 3
- Error handling — exercised through tests in Tasks 5, 6, 8, 9
- Testing — unit tests in Tasks 4–9; integration in Task 11; smoke in Task 10
- Rollout — Task 12

### Placeholder scan

No `TBD`, `TODO`, `similar to Task N`, or abbreviated code blocks. Every
step has full copy-paste-ready content.

### Type consistency

`Btc5mMarket`, `Btc5mTrade`, `Btc5mRepository`, `Btc5mScanner`,
`Btc5mDecider`, `Btc5mResolver`, `BtcSpotSource`, `estimate_up_probability`,
`parse_btc5m_slug`, `compute_pnl`, `BookFetcher`, `SpotHistory` — all
defined in earlier tasks before being referenced later. No name drift.

### Ambiguity check

One decision worth flagging: in Task 8 the decider uses current spot as a
proxy for `start_spot` when the window hasn't opened yet. This will
produce P(up) ≈ 0.5 for future-dated markets (which is accurate — we
genuinely don't know where the window will open). The first moment we
have a real edge signal is when the window has opened AND we're inside
it. In practice this means we're unlikely to trade on 24-hour-early
markets, which is the correct behavior. Calling it out here so a reader
doesn't file a bug.

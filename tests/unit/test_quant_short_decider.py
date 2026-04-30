"""Tests for the registry-aware short-horizon decider."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from polyagent.services.quant.short_horizon.decider import QuantDecider


class _FakeRepo:
    def __init__(self, open_per_asset: dict[str, int] | None = None):
        self.trades_for: dict[str, list] = {}
        self.inserted: list = []
        self.open_per_asset = open_per_asset or {}

    def get_trades_for_market(self, market_id):
        return self.trades_for.get(market_id, [])

    def insert_trade(self, t):
        self.inserted.append(t)
        # Mirror DB-side accounting so per-asset cap works across calls.
        # Trades are unresolved at insertion time; map by inferred asset.
        # We can't read asset_id off the trade, so callers update
        # self.open_per_asset directly when needed.

    def count_open_trades_for_asset(self, asset_id: str) -> int:
        return self.open_per_asset.get(asset_id, 0)


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
        return 0.0


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
    book = _FakeBook((Decimal("0.30"), Decimal("0.32")))
    d = QuantDecider(sources=sources, book=book, repo=repo, position_size_usd=Decimal("5"))
    d.evaluate(_row())
    assert len(repo.inserted) == 1
    assert repo.inserted[0].side == "YES"


def test_decider_skips_market_with_existing_trade():
    repo = _FakeRepo()
    repo.trades_for["m1"] = [{"id": "t1"}]
    sources = {"BTC": _FakeSrc(Decimal("60000"))}
    d = QuantDecider(
        sources=sources,
        book=_FakeBook((Decimal("0.4"), Decimal("0.5"))),
        repo=repo,
        position_size_usd=Decimal("5"),
    )
    d.evaluate(_row())
    assert repo.inserted == []


def test_decider_skips_when_no_spot():
    repo = _FakeRepo()
    sources = {"BTC": _FakeSrc(None)}
    d = QuantDecider(
        sources=sources,
        book=_FakeBook((Decimal("0.4"), Decimal("0.5"))),
        repo=repo,
        position_size_usd=Decimal("5"),
    )
    d.evaluate(_row())
    assert repo.inserted == []


def test_decider_skips_unknown_asset():
    repo = _FakeRepo()
    sources = {"BTC": _FakeSrc(Decimal("60000"))}
    d = QuantDecider(
        sources=sources,
        book=_FakeBook((Decimal("0.4"), Decimal("0.5"))),
        repo=repo,
        position_size_usd=Decimal("5"),
    )
    d.evaluate(_row(asset_id="DOGE"))
    assert repo.inserted == []


def test_decider_skips_when_window_closed():
    repo = _FakeRepo()
    row = _row()
    row["window_end_ts"] = datetime.now(timezone.utc) - timedelta(seconds=10)
    sources = {"BTC": _FakeSrc(Decimal("60000"))}
    d = QuantDecider(
        sources=sources,
        book=_FakeBook((Decimal("0.4"), Decimal("0.5"))),
        repo=repo,
        position_size_usd=Decimal("5"),
    )
    d.evaluate(row)
    assert repo.inserted == []


def test_decider_skips_when_edge_below_threshold():
    repo = _FakeRepo()
    sources = {"BTC": _FakeSrc(Decimal("60000"))}
    # Mid ~0.5 and p_up should be ~0.5 (no drift), edge ~ 0
    book = _FakeBook((Decimal("0.49"), Decimal("0.51")))
    d = QuantDecider(sources=sources, book=book, repo=repo, position_size_usd=Decimal("5"))
    d.evaluate(_row())
    assert repo.inserted == []


def test_decider_caps_trades_per_cycle():
    """Per-cycle cap stops the cascade where one Coinbase tick triggers
    correlated trades on every active market in the same orchestrator pass."""
    repo = _FakeRepo()
    sources = {"BTC": _FakeSrc(Decimal("60000"))}
    book = _FakeBook((Decimal("0.30"), Decimal("0.32")))  # +0.19 edge, fires
    d = QuantDecider(
        sources=sources, book=book, repo=repo,
        position_size_usd=Decimal("5"),
        max_trades_per_cycle=2,
        max_open_per_asset=99,  # disable the per-asset cap for this test
    )
    for i in range(10):
        row = _row()
        row["id"] = f"m{i}"
        row["polymarket_id"] = f"0x{i:040x}"
        d.evaluate(row)
    assert len(repo.inserted) == 2


def test_reset_cycle_clears_per_cycle_counter():
    repo = _FakeRepo()
    sources = {"BTC": _FakeSrc(Decimal("60000"))}
    book = _FakeBook((Decimal("0.30"), Decimal("0.32")))
    d = QuantDecider(
        sources=sources, book=book, repo=repo,
        position_size_usd=Decimal("5"),
        max_trades_per_cycle=2,
        max_open_per_asset=99,
    )
    for i in range(3):
        row = _row()
        row["id"] = f"a{i}"
        row["polymarket_id"] = f"0x{i:040x}"
        d.evaluate(row)
    assert len(repo.inserted) == 2

    d.reset_cycle()
    for i in range(3):
        row = _row()
        row["id"] = f"b{i}"
        row["polymarket_id"] = f"0x{(i + 100):040x}"
        d.evaluate(row)
    assert len(repo.inserted) == 4   # 2 from cycle 1 + 2 from cycle 2


def test_decider_caps_open_trades_per_asset():
    """When max_open_per_asset already-open trades exist for an asset,
    the decider refuses to open more even if edge clears the threshold."""
    repo = _FakeRepo(open_per_asset={"BTC": 3})
    sources = {"BTC": _FakeSrc(Decimal("60000"))}
    book = _FakeBook((Decimal("0.30"), Decimal("0.32")))
    d = QuantDecider(
        sources=sources, book=book, repo=repo,
        position_size_usd=Decimal("5"),
        max_trades_per_cycle=99,
        max_open_per_asset=3,
    )
    d.evaluate(_row())
    assert repo.inserted == []


def test_decider_allows_trade_when_open_under_per_asset_cap():
    repo = _FakeRepo(open_per_asset={"BTC": 2})
    sources = {"BTC": _FakeSrc(Decimal("60000"))}
    book = _FakeBook((Decimal("0.30"), Decimal("0.32")))
    d = QuantDecider(
        sources=sources, book=book, repo=repo,
        position_size_usd=Decimal("5"),
        max_trades_per_cycle=99,
        max_open_per_asset=3,
    )
    d.evaluate(_row())
    assert len(repo.inserted) == 1

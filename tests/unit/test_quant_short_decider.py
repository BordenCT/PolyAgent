"""Tests for the registry-aware short-horizon decider."""
from __future__ import annotations

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

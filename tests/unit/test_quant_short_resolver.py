"""Tests for the registry-aware short-horizon resolver."""
from __future__ import annotations

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

    def update_market_resolution(
        self, market_id, *, start_spot, end_spot, outcome, price_source_id
    ):
        self.resolved.append({
            "id": market_id,
            "start_spot": start_spot,
            "end_spot": end_spot,
            "outcome": outcome,
            "price_source_id": price_source_id,
        })

    def get_trades_for_market(self, market_id):
        return [{
            "id": "t1",
            "side": "YES",
            "fill_price_assumed": Decimal("0.40"),
            "size": Decimal("5"),
            "pnl": None,
        }]

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
        "id": "m1",
        "polymarket_id": "0xabc",
        "asset_id": "BTC",
        "window_start_ts": ws,
        "window_end_ts": we,
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
    market = {
        "id": "m1",
        "polymarket_id": "0xabc",
        "asset_id": "BTC",
        "window_start_ts": ws,
        "window_end_ts": we,
    }
    repo = _FakeRepo([market])
    settlements = {"BTC": _FakeSettlement({}, "coinbase:BTC-USD")}
    r = QuantResolver(repo=repo, settlements=settlements)
    assert r.resolve_due_markets() == 0
    assert repo.resolved == []


def test_resolver_skips_market_without_registered_settlement_source():
    ws = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    we = datetime(2026, 4, 1, 12, 5, tzinfo=timezone.utc)
    market = {
        "id": "m1",
        "polymarket_id": "0xabc",
        "asset_id": "DOGE",
        "window_start_ts": ws,
        "window_end_ts": we,
    }
    repo = _FakeRepo([market])
    r = QuantResolver(repo=repo, settlements={})
    assert r.resolve_due_markets() == 0
    assert repo.resolved == []


def test_resolver_marks_no_outcome_on_drop():
    ws = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    we = datetime(2026, 4, 1, 12, 5, tzinfo=timezone.utc)
    market = {
        "id": "m1",
        "polymarket_id": "0xabc",
        "asset_id": "BTC",
        "window_start_ts": ws,
        "window_end_ts": we,
    }
    repo = _FakeRepo([market])
    settlements = {"BTC": _FakeSettlement(
        {ws: Decimal("60000"), we: Decimal("59900")}, "coinbase:BTC-USD",
    )}
    r = QuantResolver(repo=repo, settlements=settlements)
    r.resolve_due_markets()
    assert repo.resolved[0]["outcome"] == "NO"
    # YES at 0.40 with NO outcome means -0.40 * 5 = -2.00
    assert repo.pnls["t1"] == Decimal("-2.00")

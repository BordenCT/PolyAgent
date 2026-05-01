"""Tests for the Polymarket-truth short-horizon resolver."""
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


class _FakeClient:
    def __init__(self, states: dict[str, dict | None] | None = None,
                 raises: dict[str, Exception] | None = None):
        self._states = states or {}
        self._raises = raises or {}

    def fetch_market_state(self, condition_id: str):
        if condition_id in self._raises:
            raise self._raises[condition_id]
        return self._states.get(condition_id)


class _FakeSettlement:
    def __init__(self, prices: dict, sid: str):
        self._p = prices
        self._sid = sid

    def price_at(self, ts):
        return self._p.get(ts)

    def source_id(self):
        return self._sid


def _market(pm_id="0xabc", asset="BTC"):
    ws = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    we = datetime(2026, 4, 1, 12, 5, tzinfo=timezone.utc)
    return {
        "id": "m1",
        "polymarket_id": pm_id,
        "asset_id": asset,
        "window_start_ts": ws,
        "window_end_ts": we,
    }, ws, we


def _state(midpoint: str, resolved: bool = True) -> dict:
    return {"is_resolved": resolved, "midpoint_price": Decimal(midpoint)}


class TestPolymarketTruth:
    def test_yes_outcome_from_pm(self):
        m, _, _ = _market()
        repo = _FakeRepo([m])
        client = _FakeClient({"0xabc": _state("1")})
        r = QuantResolver(repo=repo, client=client, settlements={})
        assert r.resolve_due_markets() == 1
        assert repo.resolved[0]["outcome"] == "YES"
        assert repo.resolved[0]["price_source_id"] == "polymarket:clob"
        # YES side at 0.40 with YES outcome: 5 * (1 - 0.40) = 3.00
        assert repo.pnls["t1"] == Decimal("3.00")

    def test_no_outcome_from_pm(self):
        m, _, _ = _market()
        repo = _FakeRepo([m])
        client = _FakeClient({"0xabc": _state("0")})
        r = QuantResolver(repo=repo, client=client, settlements={})
        assert r.resolve_due_markets() == 1
        assert repo.resolved[0]["outcome"] == "NO"
        # YES side at 0.40 with NO outcome: -5 * 0.40 = -2.00
        assert repo.pnls["t1"] == Decimal("-2.00")

    def test_pm_unresolved_defers(self):
        m, _, _ = _market()
        repo = _FakeRepo([m])
        client = _FakeClient({"0xabc": _state("1", resolved=False)})
        r = QuantResolver(repo=repo, client=client, settlements={})
        assert r.resolve_due_markets() == 0
        assert repo.resolved == []
        assert repo.pnls == {}

    def test_pm_returns_none_defers(self):
        m, _, _ = _market()
        repo = _FakeRepo([m])
        client = _FakeClient({"0xabc": None})
        r = QuantResolver(repo=repo, client=client, settlements={})
        assert r.resolve_due_markets() == 0
        assert repo.resolved == []

    def test_pm_fetch_exception_defers(self):
        m, _, _ = _market()
        repo = _FakeRepo([m])
        client = _FakeClient(raises={"0xabc": RuntimeError("network")})
        r = QuantResolver(repo=repo, client=client, settlements={})
        assert r.resolve_due_markets() == 0
        assert repo.resolved == []

    def test_pm_unexpected_midpoint_defers(self):
        # Closed but midpoint at 0.5 (paused or partial state). Don't act.
        m, _, _ = _market()
        repo = _FakeRepo([m])
        client = _FakeClient({"0xabc": _state("0.5")})
        r = QuantResolver(repo=repo, client=client, settlements={})
        assert r.resolve_due_markets() == 0
        assert repo.resolved == []

    def test_settlement_failure_does_not_block_resolution(self):
        # PM says YES; our settlement source has no price for these
        # timestamps. We should still record outcome=YES with None spots.
        m, _, _ = _market()
        repo = _FakeRepo([m])
        client = _FakeClient({"0xabc": _state("1")})
        settlements = {"BTC": _FakeSettlement({}, "coinbase:BTC-USD")}
        r = QuantResolver(repo=repo, client=client, settlements=settlements)
        assert r.resolve_due_markets() == 1
        row = repo.resolved[0]
        assert row["outcome"] == "YES"
        assert row["start_spot"] is None
        assert row["end_spot"] is None
        # price_source_id is the PM identifier regardless of settlement state
        assert row["price_source_id"] == "polymarket:clob"

    def test_settlement_records_diagnostic_spots(self):
        m, ws, we = _market()
        repo = _FakeRepo([m])
        client = _FakeClient({"0xabc": _state("1")})
        settlements = {"BTC": _FakeSettlement(
            {ws: Decimal("60000"), we: Decimal("60100")}, "coinbase:BTC-USD",
        )}
        r = QuantResolver(repo=repo, client=client, settlements=settlements)
        r.resolve_due_markets()
        row = repo.resolved[0]
        assert row["start_spot"] == Decimal("60000")
        assert row["end_spot"] == Decimal("60100")

    def test_pm_can_disagree_with_spot_drift(self):
        # Spot moved up 100; previously this would have forced outcome=YES.
        # Polymarket settled NO. The resolver must trust PM, not the drift.
        m, ws, we = _market()
        repo = _FakeRepo([m])
        client = _FakeClient({"0xabc": _state("0")})  # PM says NO
        settlements = {"BTC": _FakeSettlement(
            {ws: Decimal("60000"), we: Decimal("60100")}, "coinbase:BTC-USD",
        )}
        r = QuantResolver(repo=repo, client=client, settlements=settlements)
        r.resolve_due_markets()
        assert repo.resolved[0]["outcome"] == "NO"
        # YES side at 0.40 with NO: -5 * 0.40 = -2.00 (not the +3.00 the
        # old end_spot >= start_spot rule would have produced).
        assert repo.pnls["t1"] == Decimal("-2.00")

    def test_market_without_polymarket_id_is_skipped(self):
        m, _, _ = _market(pm_id="")
        repo = _FakeRepo([m])
        client = _FakeClient({})
        r = QuantResolver(repo=repo, client=client, settlements={})
        assert r.resolve_due_markets() == 0
        assert repo.resolved == []

    def test_already_resolved_trade_is_not_overwritten(self):
        m, _, _ = _market()

        class _Repo(_FakeRepo):
            def get_trades_for_market(self, market_id):
                return [
                    {"id": "t-old", "side": "YES",
                     "fill_price_assumed": Decimal("0.40"),
                     "size": Decimal("5"), "pnl": Decimal("3.00")},
                    {"id": "t-new", "side": "NO",
                     "fill_price_assumed": Decimal("0.55"),
                     "size": Decimal("5"), "pnl": None},
                ]

        repo = _Repo([m])
        client = _FakeClient({"0xabc": _state("1")})
        r = QuantResolver(repo=repo, client=client, settlements={})
        r.resolve_due_markets()
        assert "t-old" not in repo.pnls
        # NO side at 0.55 with YES outcome: -5 * 0.55 = -2.75
        assert repo.pnls["t-new"] == Decimal("-2.75")

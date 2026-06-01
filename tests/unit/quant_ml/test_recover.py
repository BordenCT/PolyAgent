"""Unit tests for shadow-label recovery primitives."""
from __future__ import annotations

from decimal import Decimal

import pytest

from polyagent.services.quant.ml.recover import _outcome_from_state


def test_outcome_yes_when_midpoint_one():
    assert _outcome_from_state(
        {"is_resolved": True, "midpoint_price": Decimal("1")}
    ) == "YES"


def test_outcome_no_when_midpoint_zero():
    assert _outcome_from_state(
        {"is_resolved": True, "midpoint_price": Decimal("0")}
    ) == "NO"


def test_outcome_none_when_unresolved():
    assert _outcome_from_state(
        {"is_resolved": False, "midpoint_price": Decimal("0.5")}
    ) is None


def test_outcome_none_when_state_missing():
    assert _outcome_from_state(None) is None


def test_outcome_none_on_open_midpoint():
    """A resolved market should pin to 0/1; an in-between midpoint is
    treated as not-yet-final (defer), matching resolver semantics."""
    assert _outcome_from_state(
        {"is_resolved": True, "midpoint_price": Decimal("0.5")}
    ) is None


def test_resolve_one_parses_slug_and_enriches(monkeypatch):
    """_resolve_one should decode the slug into window timing and attach
    asset_id even before the PM fetch."""
    from polyagent.services.quant.ml import recover

    # A real short-horizon slug: <token>-updown-<dur>-<end_unix>.
    # Registry slug_token for BTC is "btc". end_unix is a fixed UTC instant.
    slug = "btc-updown-5m-1779494400"

    class _FakeClient:
        def fetch_market_state(self, cid):
            return {"is_resolved": True, "midpoint_price": Decimal("1")}

    row = {"polymarket_id": "0xabc", "slug": slug, "decision_ts": None}
    enriched, outcome, fetch_ok = recover._resolve_one(_FakeClient(), row)
    assert fetch_ok is True
    assert outcome == "YES"
    assert enriched["asset_id"] == "BTC"
    assert enriched["window_duration_s"] == 300
    assert enriched["window_end_ts"] is not None
    assert enriched["window_start_ts"] < enriched["window_end_ts"]


def test_resolve_one_handles_unparseable_slug():
    from polyagent.services.quant.ml import recover

    class _FakeClient:
        def fetch_market_state(self, cid):  # pragma: no cover - shouldn't be called
            raise AssertionError("should not fetch on unparseable slug")

    row = {"polymarket_id": "0xabc", "slug": "not-a-valid-slug", "decision_ts": None}
    enriched, outcome, fetch_ok = recover._resolve_one(_FakeClient(), row)
    # Unparseable: no asset_id added, no outcome, not flagged as fetch failure.
    assert "asset_id" not in enriched
    assert outcome is None
    assert fetch_ok is True

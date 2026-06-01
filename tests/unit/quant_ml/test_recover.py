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


def test_resolve_one_failed_fetch_is_not_silently_unresolved():
    """A None state (exhausted-429 / network failure) must report
    fetch_ok=False, so a rate-limit storm is counted as fetch_failed and
    retried on a re-run rather than poisoning the label set as 'unresolved'."""
    from polyagent.services.quant.ml import recover

    class _DeadClient:
        def fetch_market_state(self, cid):
            return None  # fetch failed

    row = {"polymarket_id": "0xabc", "slug": "btc-updown-5m-1779494400",
           "decision_ts": None}
    # attempts=1 keeps the test fast (no backoff sleep).
    enriched, outcome, fetch_ok = recover._resolve_one(_DeadClient(), row, attempts=1)
    assert enriched["asset_id"] == "BTC"
    assert outcome is None
    assert fetch_ok is False


def test_fetch_with_retry_retries_then_succeeds():
    from polyagent.services.quant.ml import recover

    calls = {"n": 0}

    class _FlakyClient:
        def fetch_market_state(self, cid):
            calls["n"] += 1
            if calls["n"] < 3:
                return None
            return {"is_resolved": True, "midpoint_price": Decimal("1")}

    slept = []
    state = recover._fetch_with_retry(
        _FlakyClient(), "0xabc", recover._NULL_GATE,
        attempts=4, sleep=slept.append, jitter=lambda: 0.0,
    )
    assert state == {"is_resolved": True, "midpoint_price": Decimal("1")}
    assert calls["n"] == 3
    assert slept == [1.0, 2.0]  # backoff between the two failed attempts


def test_fetch_with_retry_gives_up_after_attempts():
    from polyagent.services.quant.ml import recover

    calls = {"n": 0}

    class _DeadClient:
        def fetch_market_state(self, cid):
            calls["n"] += 1
            return None

    slept = []
    state = recover._fetch_with_retry(
        _DeadClient(), "0xabc", recover._NULL_GATE,
        attempts=3, sleep=slept.append, jitter=lambda: 0.0,
    )
    assert state is None
    assert calls["n"] == 3
    assert slept == [1.0, 2.0]  # no sleep after the final attempt


def test_rate_gate_assigns_spaced_slots():
    """Each wait() schedules the next caller one interval later; the sleep
    is the gap to that caller's assigned slot."""
    from polyagent.services.quant.ml.recover import _RateGate

    clock = [100.0]
    slept = []
    gate = _RateGate(10.0, monotonic=lambda: clock[0], sleep=slept.append)
    gate.wait()  # slot=100.0, delay 0 -> no sleep
    gate.wait()  # slot=100.1, delay 0.1 -> sleep 0.1
    gate.wait()  # slot=100.2, delay 0.2 -> sleep 0.2
    assert slept == [pytest.approx(0.1), pytest.approx(0.2)]


def test_rate_gate_disabled_when_rate_non_positive():
    from polyagent.services.quant.ml.recover import _RateGate

    slept = []
    gate = _RateGate(0, monotonic=lambda: 0.0, sleep=slept.append)
    gate.wait()
    gate.wait()
    assert slept == []

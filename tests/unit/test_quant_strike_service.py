"""Unit tests for QuantStrikeService."""
from __future__ import annotations

from decimal import Decimal

from polyagent.services.quant.strike.service import QuantStrikeService


class _FakeSource:
    def __init__(self, current: Decimal | None):
        self._cur = current

    def current(self) -> Decimal | None:
        return self._cur

    def realized_vol(self, window_s: int) -> float:
        return 0.0  # forces fixed-vol fallback via HYBRID/long horizon


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

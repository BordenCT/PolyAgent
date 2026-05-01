"""Tests for ChainlinkDataFeedSource."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from polyagent.services.quant.assets.sources.chainlink import (
    ChainlinkDataFeedSource,
    POLYGON_AGGREGATORS,
    _decode_round_data,
    _encode_round_call,
)


def _hex_round(round_id: int, answer: int, started: int, updated: int,
               answered_in: int) -> str:
    """Encode the 5-tuple AggregatorV3 return as a hex eth_call result."""
    return "0x" + (
        round_id.to_bytes(32, "big").hex()
        + answer.to_bytes(32, "big", signed=True).hex()
        + started.to_bytes(32, "big").hex()
        + updated.to_bytes(32, "big").hex()
        + answered_in.to_bytes(32, "big").hex()
    )


def _resp(result: str | None = None, error: dict | None = None,
          status: int = 200):
    body = {"jsonrpc": "2.0", "id": 1}
    if result is not None:
        body["result"] = result
    if error is not None:
        body["error"] = error
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=body)
    r.raise_for_status = MagicMock()
    return r


class _RpcStub:
    """Minimal httpx.Client stub that returns canned responses by call order
    or by matching the call's `data` field (for getRoundData walks)."""

    def __init__(self, latest_response, by_round=None):
        self.latest_response = latest_response
        self.by_round = by_round or {}
        self.calls = []

    def post(self, url, json=None):
        self.calls.append(json)
        data = json["params"][0]["data"]
        if data == "0xfeaf968c":
            return self.latest_response
        # getRoundData(uint80): selector + 32-byte big-endian roundId
        round_id = int(data[10:], 16)
        if round_id in self.by_round:
            return self.by_round[round_id]
        return _resp(error={"message": f"unknown round {round_id}"})

    def close(self):
        pass


class TestEncodingDecoding:
    def test_round_call_encodes_selector_and_padded_round_id(self):
        encoded = _encode_round_call(42)
        assert encoded.startswith("0x9a6fc8f5")
        # round_id should be right-aligned in a 32-byte slot
        assert encoded[10:] == "00" * 31 + "2a"

    def test_decode_round_data_extracts_all_five_fields(self):
        hex_result = _hex_round(
            round_id=100, answer=110_500_000_000,  # $1105.00 at 8 decimals... wait
            started=1_700_000_000, updated=1_700_000_060, answered_in=100,
        )
        rid, answer, started, updated, answered_in = _decode_round_data(hex_result)
        assert rid == 100
        assert answer == 110_500_000_000
        assert started == 1_700_000_000
        assert updated == 1_700_000_060
        assert answered_in == 100

    def test_decode_round_data_handles_signed_negative_answer(self):
        # Theoretically possible if a feed reports a negative reading.
        # Encoded as two's complement.
        neg = -42
        hex_result = "0x" + (
            (1).to_bytes(32, "big").hex()
            + neg.to_bytes(32, "big", signed=True).hex()
            + (0).to_bytes(32, "big").hex()
            + (0).to_bytes(32, "big").hex()
            + (1).to_bytes(32, "big").hex()
        )
        _, answer, _, _, _ = _decode_round_data(hex_result)
        assert answer == -42

    def test_decode_round_data_rejects_truncated_payload(self):
        with pytest.raises(ValueError):
            _decode_round_data("0xdeadbeef")


class TestSourceMetadata:
    def test_source_id_includes_chain_and_pair(self):
        s = ChainlinkDataFeedSource(pair="BTC-USD", http_client=_RpcStub(_resp()))
        assert s.source_id() == "chainlink:polygon:BTC-USD"

    def test_unknown_pair_raises(self):
        with pytest.raises(ValueError):
            ChainlinkDataFeedSource(pair="DOGE-USD", http_client=_RpcStub(_resp()))

    def test_explicit_aggregator_overrides_default(self):
        custom = "0x" + "ff" * 20
        s = ChainlinkDataFeedSource(
            pair="BTC-USD", aggregator_address=custom,
            http_client=_RpcStub(_resp()),
        )
        assert s._addr == custom

    def test_polygon_aggregators_constants_are_present(self):
        assert "BTC-USD" in POLYGON_AGGREGATORS
        assert "ETH-USD" in POLYGON_AGGREGATORS


class TestTickAndCurrent:
    def test_tick_returns_scaled_price_and_buffers_it(self):
        # 6_500_000_000_000 / 10^8 = 65000.00
        latest = _resp(_hex_round(100, 6_500_000_000_000, 0, 1_700_000_000, 100))
        s = ChainlinkDataFeedSource(pair="BTC-USD", http_client=_RpcStub(latest))
        price = s.tick()
        assert price == Decimal("65000")
        assert s.current() == Decimal("65000")

    def test_tick_rpc_error_returns_none(self):
        class _Failing:
            def post(self, *a, **kw):
                raise RuntimeError("boom")
            def close(self):
                pass

        s = ChainlinkDataFeedSource(pair="BTC-USD", http_client=_Failing())
        assert s.tick() is None
        assert s.current() is None

    def test_current_returns_none_before_first_tick(self):
        latest = _resp(_hex_round(1, 1, 0, 1, 1))
        s = ChainlinkDataFeedSource(pair="BTC-USD", http_client=_RpcStub(latest))
        assert s.current() is None


class TestPriceAtRoundWalk:
    def test_returns_latest_when_target_is_in_the_future(self):
        # latest updated_at = 1_700_000_060; target = 1_700_001_000 (future)
        latest = _resp(_hex_round(100, 6_500_000_000_000, 0, 1_700_000_060, 100))
        s = ChainlinkDataFeedSource(pair="BTC-USD", http_client=_RpcStub(latest))
        ts = datetime.fromtimestamp(1_700_001_000, tz=timezone.utc)
        assert s.price_at(ts) == Decimal("65000")

    def test_walks_back_to_round_at_or_before_target(self):
        # Latest is at t=1_700_000_300 (round 103). Target = t=1_700_000_120.
        # Round 102 updated at 1_700_000_240 (still > target).
        # Round 101 updated at 1_700_000_180 (still > target).
        # Round 100 updated at 1_700_000_120 (== target). Return its price.
        latest = _resp(_hex_round(103, 6_503_000_000_000, 0, 1_700_000_300, 103))
        by_round = {
            102: _resp(_hex_round(102, 6_502_000_000_000, 0, 1_700_000_240, 102)),
            101: _resp(_hex_round(101, 6_501_000_000_000, 0, 1_700_000_180, 101)),
            100: _resp(_hex_round(100, 6_500_000_000_000, 0, 1_700_000_120, 100)),
        }
        s = ChainlinkDataFeedSource(
            pair="BTC-USD", http_client=_RpcStub(latest, by_round=by_round),
        )
        ts = datetime.fromtimestamp(1_700_000_120, tz=timezone.utc)
        assert s.price_at(ts) == Decimal("65000")

    def test_returns_none_when_walk_exhausts_without_finding_round(self):
        # Latest is way ahead of target, all earlier rounds also after target.
        latest = _resp(_hex_round(2, 1, 0, 1_000_000_000, 2))
        # Stub round 1 to updated_at also after target so walk fails to find.
        by_round = {1: _resp(_hex_round(1, 1, 0, 999_999_999, 1))}
        s = ChainlinkDataFeedSource(
            pair="BTC-USD", http_client=_RpcStub(latest, by_round=by_round),
        )
        ts = datetime.fromtimestamp(1, tz=timezone.utc)
        # round_id will go below 0 quickly: walk returns None.
        assert s.price_at(ts) is None

    def test_rpc_error_during_walk_returns_none(self):
        latest = _resp(_hex_round(5, 1, 0, 1_000_000_000, 5))
        # Round 4 returns an RPC-level error.
        by_round = {4: _resp(error={"message": "node down"})}
        s = ChainlinkDataFeedSource(
            pair="BTC-USD", http_client=_RpcStub(latest, by_round=by_round),
        )
        ts = datetime.fromtimestamp(0, tz=timezone.utc)
        assert s.price_at(ts) is None

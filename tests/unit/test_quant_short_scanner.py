"""Tests for the registry-aware short-horizon scanner."""
from __future__ import annotations

import json

import pytest

from polyagent.services.quant.short_horizon.scanner import (
    QuantShortScanner,
    parse_short_horizon_slug,
)


def test_parse_btc_5m_slug():
    end_ts = 1_900_000_000
    slug = f"btc-updown-5m-{end_ts}"
    asset_id, ws, we, dur = parse_short_horizon_slug(slug)
    assert asset_id == "BTC"
    assert dur == 300
    assert int(we.timestamp()) == end_ts
    assert int(ws.timestamp()) == end_ts - 300


def test_eth_slug_does_not_match_when_not_short_horizon():
    """ETH is registered for STRIKE/RANGE only, not SHORT_HORIZON."""
    with pytest.raises(ValueError):
        parse_short_horizon_slug("eth-updown-15m-1900000000")


def test_parse_unknown_asset_raises():
    with pytest.raises(ValueError):
        parse_short_horizon_slug("doge-updown-5m-1900000000")


class _FakeHttp:
    def __init__(self, body):
        self._body = body
        self.last_params = None

    def get(self, url, params=None):
        self.last_params = params
        outer = self

        class R:
            status_code = 200

            def json(self_inner):
                return outer._body

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
    assert out[0].polymarket_id == "0xabc"
    assert out[0].token_id_yes == "yes_id"
    assert out[0].token_id_no == "no_id"


def test_scanner_skips_non_matching_slugs():
    body = [
        {"slug": "doge-updown-5m-1900000000", "conditionId": "0x1",
         "clobTokenIds": json.dumps(["a", "b"])},
        {"slug": "some-unrelated-market", "conditionId": "0x2",
         "clobTokenIds": json.dumps(["c", "d"])},
    ]
    s = QuantShortScanner(http_client=_FakeHttp(body))
    assert s.scan() == []


def test_scanner_handles_bad_token_ids_gracefully():
    end_ts = 1_900_000_000
    body = [{
        "slug": f"btc-updown-5m-{end_ts}",
        "conditionId": "0xabc",
        "clobTokenIds": json.dumps(["only_one"]),
    }]
    s = QuantShortScanner(http_client=_FakeHttp(body))
    assert s.scan() == []

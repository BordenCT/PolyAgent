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
        "outcomes": json.dumps(["Up", "Down"]),
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
         "clobTokenIds": json.dumps(["a", "b"]),
         "outcomes": json.dumps(["Up", "Down"])},
        {"slug": "some-unrelated-market", "conditionId": "0x2",
         "clobTokenIds": json.dumps(["c", "d"]),
         "outcomes": json.dumps(["Up", "Down"])},
    ]
    s = QuantShortScanner(http_client=_FakeHttp(body))
    assert s.scan() == []


def test_scanner_handles_bad_token_ids_gracefully():
    end_ts = 1_900_000_000
    body = [{
        "slug": f"btc-updown-5m-{end_ts}",
        "conditionId": "0xabc",
        "clobTokenIds": json.dumps(["only_one"]),
        "outcomes": json.dumps(["Up", "Down"]),
    }]
    s = QuantShortScanner(http_client=_FakeHttp(body))
    assert s.scan() == []


def test_scanner_pairs_tokens_by_outcome_label_not_position():
    """Regression: if Gamma returns outcomes in [Down, Up] order, the
    scanner must still bind token_id_yes to the Up-token, not blindly
    take token_ids[0]. Otherwise every trade flips silently."""
    end_ts = 1_900_000_000
    body = [{
        "slug": f"btc-updown-5m-{end_ts}",
        "conditionId": "0xabc",
        # Order intentionally inverted vs the prior test.
        "clobTokenIds": json.dumps(["down_id", "up_id"]),
        "outcomes": json.dumps(["Down", "Up"]),
    }]
    s = QuantShortScanner(http_client=_FakeHttp(body))
    out = s.scan()
    assert len(out) == 1
    assert out[0].token_id_yes == "up_id"
    assert out[0].token_id_no == "down_id"


def test_scanner_skips_market_with_missing_outcomes():
    end_ts = 1_900_000_000
    body = [{
        "slug": f"btc-updown-5m-{end_ts}",
        "conditionId": "0xabc",
        "clobTokenIds": json.dumps(["a", "b"]),
        # outcomes field absent
    }]
    s = QuantShortScanner(http_client=_FakeHttp(body))
    assert s.scan() == []


def test_scanner_skips_market_with_unrecognised_outcomes():
    """Categorical labels like Trump/Harris should not be entered as
    binary up/down trades, even if the slug regex coincidentally
    matched (which it shouldn't, but defense in depth)."""
    end_ts = 1_900_000_000
    body = [{
        "slug": f"btc-updown-5m-{end_ts}",
        "conditionId": "0xabc",
        "clobTokenIds": json.dumps(["a", "b"]),
        "outcomes": json.dumps(["Trump", "Harris"]),
    }]
    s = QuantShortScanner(http_client=_FakeHttp(body))
    assert s.scan() == []


def test_scanner_accepts_yes_no_labels():
    """Some PM markets use Yes/No instead of Up/Down for the same
    semantic axis."""
    end_ts = 1_900_000_000
    body = [{
        "slug": f"btc-updown-5m-{end_ts}",
        "conditionId": "0xabc",
        "clobTokenIds": json.dumps(["yes_id", "no_id"]),
        "outcomes": json.dumps(["Yes", "No"]),
    }]
    s = QuantShortScanner(http_client=_FakeHttp(body))
    out = s.scan()
    assert len(out) == 1
    assert out[0].token_id_yes == "yes_id"
    assert out[0].token_id_no == "no_id"


def test_scanner_requests_newest_first():
    """Regression: gamma's default ordering pushes rapidly-rotating 5m
    markets out of the page_limit window. We must request startDate desc."""
    http = _FakeHttp([])
    QuantShortScanner(http_client=http).scan()
    assert http.last_params["order"] == "startDate"
    assert http.last_params["ascending"] == "false"

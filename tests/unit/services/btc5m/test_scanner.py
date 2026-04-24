"""Tests for the BTC 5m market scanner and slug parser."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from polyagent.services.btc5m.scanner import (
    BTC5M_SLUG_RE,
    parse_btc5m_slug,
    Btc5mScanner,
)


class TestSlugParser:
    def test_valid_5m_slug(self):
        m = BTC5M_SLUG_RE.match("btc-updown-5m-1776995400")
        assert m is not None
        assert m.group(1) == "5m"
        assert m.group(2) == "1776995400"

    def test_valid_15m_slug(self):
        m = BTC5M_SLUG_RE.match("btc-updown-15m-1776995400")
        assert m is not None
        assert m.group(1) == "15m"
        assert m.group(2) == "1776995400"

    def test_parse_5m_returns_300s_window(self):
        window_start, window_end, duration = parse_btc5m_slug("btc-updown-5m-1776995400")
        assert window_end == datetime(2026, 4, 24, 1, 50, tzinfo=timezone.utc)
        assert duration == 300
        assert (window_end - window_start).total_seconds() == 300

    def test_parse_15m_returns_900s_window(self):
        window_start, window_end, duration = parse_btc5m_slug("btc-updown-15m-1776995400")
        assert duration == 900
        assert (window_end - window_start).total_seconds() == 900

    def test_parse_1h_returns_3600s_window(self):
        _, _, duration = parse_btc5m_slug("btc-updown-1h-1776995400")
        assert duration == 3600

    def test_parse_1d_returns_86400s_window(self):
        _, _, duration = parse_btc5m_slug("btc-updown-1d-1776995400")
        assert duration == 86400

    def test_rejects_wrong_asset(self):
        with pytest.raises(ValueError):
            parse_btc5m_slug("eth-updown-5m-1776995400")

    def test_rejects_malformed_duration(self):
        with pytest.raises(ValueError):
            parse_btc5m_slug("btc-updown-fast-1776995400")

    def test_rejects_malformed_timestamp(self):
        with pytest.raises(ValueError):
            parse_btc5m_slug("btc-updown-5m-not-a-number")


class TestBtc5mScanner:
    def _make_gamma_response(self, slug: str, polymarket_id: str):
        return [{
            "conditionId": polymarket_id,
            "slug": slug,
            "question": "Bitcoin Up or Down - X",
            "clobTokenIds": json.dumps(["t_yes", "t_no"]),
            "endDate": "2026-04-24T01:50:00Z",
            "active": True,
            "closed": False,
        }]

    def test_scan_parses_one_market(self):
        http = MagicMock()
        http.get.return_value.status_code = 200
        http.get.return_value.json.return_value = self._make_gamma_response(
            "btc-updown-5m-1776995400", "0xabc"
        )
        scanner = Btc5mScanner(http_client=http)
        markets = scanner.scan()
        assert len(markets) == 1
        m = markets[0]
        assert m.polymarket_id == "0xabc"
        assert m.slug == "btc-updown-5m-1776995400"
        assert m.token_id_yes == "t_yes"
        assert m.token_id_no == "t_no"
        assert m.window_duration_s == 300

    def test_scan_accepts_5m_and_15m_rejects_other_assets(self):
        http = MagicMock()
        http.get.return_value.status_code = 200
        http.get.return_value.json.return_value = [
            {"conditionId": "0x1", "slug": "btc-updown-5m-1776995400",
             "clobTokenIds": json.dumps(["a","b"]), "endDate": "2026-04-24T01:50:00Z"},
            {"conditionId": "0x2", "slug": "btc-updown-15m-1776995400",
             "clobTokenIds": json.dumps(["c","d"]), "endDate": "2026-04-24T01:50:00Z"},
            {"conditionId": "0x3", "slug": "some-other-market",
             "clobTokenIds": json.dumps(["e","f"]), "endDate": "2026-04-24T01:50:00Z"},
            {"conditionId": "0x4", "slug": "eth-updown-5m-1776995400",
             "clobTokenIds": json.dumps(["g","h"]), "endDate": "2026-04-24T01:50:00Z"},
        ]
        scanner = Btc5mScanner(http_client=http)
        markets = scanner.scan()
        assert len(markets) == 2
        ids = {m.polymarket_id for m in markets}
        assert ids == {"0x1", "0x2"}
        by_slug = {m.slug: m for m in markets}
        assert by_slug["btc-updown-5m-1776995400"].window_duration_s == 300
        assert by_slug["btc-updown-15m-1776995400"].window_duration_s == 900

    def test_scan_empty_on_http_error(self):
        http = MagicMock()
        http.get.side_effect = RuntimeError("nope")
        scanner = Btc5mScanner(http_client=http)
        assert scanner.scan() == []

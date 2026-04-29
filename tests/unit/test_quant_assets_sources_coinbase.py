from datetime import datetime, timezone
from decimal import Decimal

from polyagent.services.quant.assets.sources.coinbase import CoinbaseSpotSource


class _FakeHttp:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[str] = []
    def get(self, url, params=None):
        self.calls.append(url)
        if not self._responses:
            raise RuntimeError("exhausted")
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


class _Resp:
    def __init__(self, status_code: int, json_body):
        self.status_code = status_code
        self._body = json_body
    def json(self):
        return self._body
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


def test_source_id_format():
    s = CoinbaseSpotSource("BTC-USD", http_client=_FakeHttp([]))
    assert s.source_id() == "coinbase:BTC-USD"
    s2 = CoinbaseSpotSource("ETH-USD", http_client=_FakeHttp([]))
    assert s2.source_id() == "coinbase:ETH-USD"


def test_tick_appends_to_buffer_and_returns_mid():
    http = _FakeHttp([_Resp(200, {"bid": "100.00", "ask": "102.00"})])
    s = CoinbaseSpotSource("BTC-USD", http_client=http)
    p = s.tick()
    assert p == Decimal("101.00")
    assert s.current() == Decimal("101.00")


def test_tick_returns_none_on_error():
    http = _FakeHttp([RuntimeError("boom")])
    s = CoinbaseSpotSource("BTC-USD", http_client=http)
    assert s.tick() is None
    assert s.current() is None


def test_price_at_uses_candle_endpoint():
    target = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    candle = [int(target.timestamp()), 100.0, 105.0, 99.0, 103.0, 1.0]
    http = _FakeHttp([_Resp(200, [candle])])
    s = CoinbaseSpotSource("BTC-USD", http_client=http)
    assert s.price_at(target) == Decimal("103.0")

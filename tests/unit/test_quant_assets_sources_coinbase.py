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


def test_recent_return_returns_none_when_buffer_too_short():
    """Without at least one buffered tick beyond ``seconds_back`` the helper
    has no anchor to compare against and reports None."""
    s = CoinbaseSpotSource("BTC-USD", http_client=_FakeHttp([]))
    assert s.recent_return(seconds_back=300) is None


def test_recent_return_uses_in_memory_buffer_no_network():
    """recent_return must walk the existing _buf rather than re-fetching;
    the in-memory buffer is the whole point of the helper."""
    http = _FakeHttp([])  # if any call escapes to http, FakeHttp raises
    s = CoinbaseSpotSource("BTC-USD", http_client=http)
    # Seed the buffer manually with two timestamped prices.
    # 600s ago: $100, now: $110 -> simple return = +10%.
    now = 1_000_000.0
    s._buf.append((now - 600, Decimal("100")))
    s._buf.append((now, Decimal("110")))
    r = s.recent_return(seconds_back=300, _now=now)
    assert r is not None
    # 5-min lookback only has one buffered point at 600s ago, which is
    # outside the window. Helper picks the oldest sample inside the
    # window: only the latest sample qualifies, and you can't return-vs-self.
    # So 5m return is None; 10m (600s) return is +0.10.
    r2 = s.recent_return(seconds_back=600, _now=now)
    assert r2 is not None and abs(r2 - 0.10) < 1e-9


def test_recent_return_picks_closest_sample_inside_window():
    """When multiple buffered samples sit before the lookback target, the
    helper uses the one closest to the target so the return measures the
    intended horizon, not the buffer's tail."""
    s = CoinbaseSpotSource("BTC-USD", http_client=_FakeHttp([]))
    now = 1_000_000.0
    # Samples at -1800s, -900s, -300s, now. We want a 900s lookback.
    s._buf.append((now - 1800, Decimal("80")))
    s._buf.append((now - 900,  Decimal("100")))
    s._buf.append((now - 300,  Decimal("105")))
    s._buf.append((now,        Decimal("110")))
    r = s.recent_return(seconds_back=900, _now=now)
    assert r is not None
    # Latest 110 / closest-to-(-900) sample 100 - 1 = 0.10.
    assert abs(r - 0.10) < 1e-9

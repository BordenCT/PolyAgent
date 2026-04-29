"""Integration tests for the quant orchestrator loop."""
from __future__ import annotations

import queue
import threading
import time
from decimal import Decimal

from polyagent.services.quant.orchestrator import run_quant_orchestrator


class _FakeSpec:
    def __init__(self, asset_id, tick_interval_s=0.05):
        self.asset_id = asset_id
        self.tick_interval_s = tick_interval_s


class _FakeSrc:
    def __init__(self):
        self.ticks = 0
        self.closed = False

    def tick(self):
        self.ticks += 1
        return Decimal("100")

    def current(self):
        return Decimal("100") if self.ticks > 0 else None

    def realized_vol(self, window_s):
        return 0.0

    def close(self):
        self.closed = True


class _RaisingSrc(_FakeSrc):
    def tick(self):
        raise RuntimeError("upstream is down")


def test_orchestrator_isolates_failing_source():
    """A raising source does not block the other source."""
    sources = {"BTC": _FakeSrc(), "ETH": _RaisingSrc()}
    specs = [_FakeSpec("BTC"), _FakeSpec("ETH")]
    shutdown = queue.Queue()

    def stop_after(secs):
        time.sleep(secs)
        shutdown.put("stop")

    threading.Thread(target=stop_after, args=(0.3,), daemon=True).start()

    run_quant_orchestrator(
        sources=sources,
        specs=specs,
        scan_and_decide=lambda: None,
        market_interval_s=10.0,
        shutdown_q=shutdown,
    )

    assert sources["BTC"].ticks > 0
    # close() must run for every source even when one of them raises during tick
    assert sources["BTC"].closed is True
    assert sources["ETH"].closed is True


def test_orchestrator_calls_scan_and_decide():
    """scan_and_decide fires at least once when market_interval is short."""
    sources = {"BTC": _FakeSrc()}
    specs = [_FakeSpec("BTC")]
    shutdown = queue.Queue()
    scan_calls = {"n": 0}

    def scan_and_decide():
        scan_calls["n"] += 1

    def stop_after(secs):
        time.sleep(secs)
        shutdown.put("stop")

    threading.Thread(target=stop_after, args=(0.3,), daemon=True).start()

    run_quant_orchestrator(
        sources=sources,
        specs=specs,
        scan_and_decide=scan_and_decide,
        market_interval_s=0.05,
        shutdown_q=shutdown,
    )
    assert scan_calls["n"] >= 1


def test_orchestrator_isolates_failing_scan_and_decide():
    """A raising scan_and_decide does not stop the loop."""
    sources = {"BTC": _FakeSrc()}
    specs = [_FakeSpec("BTC")]
    shutdown = queue.Queue()

    def boom():
        raise RuntimeError("scan failed")

    def stop_after(secs):
        time.sleep(secs)
        shutdown.put("stop")

    threading.Thread(target=stop_after, args=(0.3,), daemon=True).start()

    run_quant_orchestrator(
        sources=sources,
        specs=specs,
        scan_and_decide=boom,
        market_interval_s=0.05,
        shutdown_q=shutdown,
    )
    # Source still ticked despite the scan failure
    assert sources["BTC"].ticks > 0

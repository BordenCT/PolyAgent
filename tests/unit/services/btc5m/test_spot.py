"""Tests for BtcSpotSource — ring buffer, tick, realized vol."""
from __future__ import annotations
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from polyagent.services.btc5m.spot import BtcSpotSource


class TestBtcSpotSource:
    def test_current_is_none_before_first_tick(self):
        src = BtcSpotSource()
        assert src.current() is None

    def test_tick_stores_price(self):
        src = BtcSpotSource()
        with patch.object(src, "_fetch_ticker", return_value=Decimal("65000.12")):
            p = src.tick()
        assert p == Decimal("65000.12")
        assert src.current() == Decimal("65000.12")

    def test_realized_vol_returns_zero_with_flat_ticks(self):
        src = BtcSpotSource()
        now = 1_000_000.0
        prices = [Decimal("65000")] * 10
        with patch("time.time", side_effect=[now + i for i in range(10)]):
            with patch.object(src, "_fetch_ticker", side_effect=prices):
                for _ in range(10):
                    src.tick()
        assert src.realized_vol(window_s=60) == pytest.approx(0.0, abs=1e-9)

    def test_realized_vol_positive_with_varying_ticks(self):
        src = BtcSpotSource()
        now = 1_000_000.0
        # Synthetic price path — alternating up/down ~0.1% steps
        prices = [
            Decimal(str(65000 * (1 + (0.001 if i % 2 == 0 else -0.001))))
            for i in range(30)
        ]
        # 1s apart
        with patch("time.time", side_effect=[now + i for i in range(30)]):
            with patch.object(src, "_fetch_ticker", side_effect=prices):
                for _ in range(30):
                    src.tick()
        vol = src.realized_vol(window_s=30)
        # With 0.1% swings every 1s, annualised vol is very large — just assert positive
        assert vol > 0.0

    def test_realized_vol_returns_zero_with_too_few_samples(self):
        src = BtcSpotSource()
        with patch.object(src, "_fetch_ticker", return_value=Decimal("65000")):
            src.tick()  # only one sample
        assert src.realized_vol(window_s=60) == 0.0

    def test_ring_buffer_caps_at_one_hour(self):
        src = BtcSpotSource(_max_age_s=3600)
        # 3700 ticks at 1s each — buffer should retain only last ~3600
        now = 1_000_000.0
        with patch("time.time", side_effect=[now + i for i in range(3700)]):
            with patch.object(src, "_fetch_ticker", return_value=Decimal("65000")):
                for _ in range(3700):
                    src.tick()
        assert len(src._buf) <= 3601

    def test_tick_swallows_http_errors(self):
        src = BtcSpotSource()
        with patch.object(src, "_fetch_ticker", side_effect=RuntimeError("boom")):
            result = src.tick()
        assert result is None
        assert src.current() is None

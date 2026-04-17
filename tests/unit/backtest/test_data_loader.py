"""Tests for backtest data loader."""
from datetime import date, datetime, timezone
from decimal import Decimal
from io import StringIO

import pytest

from polyagent.backtest.data_loader import DataLoader, MarketSnapshot


class TestDataLoader:
    def test_parse_trade_row(self):
        row = {
            "condition_id": "0xabc",
            "question": "Will BTC hit 100k?",
            "category": "crypto",
            "token_id": "tok_1",
            "price": "0.45",
            "size": "500",
            "timestamp": "2025-06-15T12:00:00Z",
            "maker": "0xwallet1",
            "outcome": "Yes",
        }
        snapshot = DataLoader.parse_trade_row(row)
        assert snapshot.polymarket_id == "0xabc"
        assert snapshot.price == Decimal("0.45")
        assert snapshot.volume == Decimal("500")

    def test_group_by_day(self):
        snapshots = [
            MarketSnapshot(
                polymarket_id="0x1", question="test?", category="crypto",
                token_id="t1", price=Decimal("0.4"), volume=Decimal("100"),
                timestamp=datetime(2025, 6, 15, 10, 0, tzinfo=timezone.utc),
                outcome="Yes",
            ),
            MarketSnapshot(
                polymarket_id="0x1", question="test?", category="crypto",
                token_id="t1", price=Decimal("0.45"), volume=Decimal("200"),
                timestamp=datetime(2025, 6, 15, 14, 0, tzinfo=timezone.utc),
                outcome="Yes",
            ),
            MarketSnapshot(
                polymarket_id="0x1", question="test?", category="crypto",
                token_id="t1", price=Decimal("0.5"), volume=Decimal("300"),
                timestamp=datetime(2025, 6, 16, 10, 0, tzinfo=timezone.utc),
                outcome="Yes",
            ),
        ]
        by_day = DataLoader.group_by_day(snapshots)
        assert len(by_day) == 2
        assert date(2025, 6, 15) in by_day
        assert len(by_day[date(2025, 6, 15)]) == 2


class TestMarketSnapshot:
    def test_to_market_data(self):
        snap = MarketSnapshot(
            polymarket_id="0x1", question="test?", category="crypto",
            token_id="t1", price=Decimal("0.45"), volume=Decimal("50000"),
            timestamp=datetime(2025, 6, 15, tzinfo=timezone.utc),
            outcome="Yes",
        )
        market = snap.to_market_data(hours_to_resolution=48.0)
        assert market.polymarket_id == "0x1"
        assert market.midpoint_price == Decimal("0.45")

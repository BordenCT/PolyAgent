"""Tests for the hourly-bar data loader."""
from datetime import datetime, timezone
from decimal import Decimal

from polyagent.backtest.data_loader import DataLoader, HourlyBar


def _bar(market_id: str, hour: datetime, close: str = "0.45") -> HourlyBar:
    price = Decimal(close)
    return HourlyBar(
        market_id=market_id,
        hour=hour,
        open=price,
        close=price,
        high=price,
        low=price,
        volume=Decimal("100"),
        first_ts=hour,
        last_ts=hour,
    )


class TestGroupByHour:
    def test_groups_bars_by_hour(self):
        h1 = datetime(2025, 6, 15, 10, tzinfo=timezone.utc)
        h2 = datetime(2025, 6, 15, 11, tzinfo=timezone.utc)
        bars = [_bar("0x1", h1), _bar("0x2", h1), _bar("0x1", h2)]
        grouped = DataLoader.group_by_hour(bars)
        assert set(grouped.keys()) == {h1, h2}
        assert len(grouped[h1]) == 2
        assert len(grouped[h2]) == 1


class TestHourlyBarToMarketData:
    def test_projects_fields(self):
        hour = datetime(2025, 6, 15, 12, tzinfo=timezone.utc)
        bar = HourlyBar(
            market_id="0x1",
            hour=hour,
            open=Decimal("0.40"),
            close=Decimal("0.45"),
            high=Decimal("0.48"),
            low=Decimal("0.39"),
            volume=Decimal("800"),
            first_ts=hour,
            last_ts=hour,
            question="Will X?",
            category="crypto",
            token_id="tok_1",
        )
        market = bar.to_market_data(hours_to_resolution=24.0, volume_24h=Decimal("15000"))
        assert market.midpoint_price == Decimal("0.45")
        assert market.volume_24h == Decimal("15000")
        assert market.hours_to_resolution == 24.0
        assert market.category == "crypto"

"""Tests for Btc5mDecider."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

from polyagent.models import Btc5mMarket
from polyagent.services.btc5m.decider import Btc5mDecider


def _make_market(now: datetime, ttm_s: int = 120) -> tuple[dict, Btc5mMarket]:
    """Produce a dict-shaped 'active market' row and a model for constructing it."""
    window_end = now + timedelta(seconds=ttm_s)
    window_start = window_end - timedelta(seconds=300)
    market_id = uuid4()
    row = {
        "id": market_id,
        "polymarket_id": "0x1",
        "slug": "btc-updown-5m-1234567890",
        "token_id_yes": "y", "token_id_no": "n",
        "window_duration_s": 300,
        "window_start_ts": window_start, "window_end_ts": window_end,
        "start_spot": None, "end_spot": None, "outcome": None,
    }
    model = Btc5mMarket(
        polymarket_id="0x1", slug=row["slug"], token_id_yes="y", token_id_no="n",
        window_duration_s=300,
        window_start_ts=window_start, window_end_ts=window_end,
    )
    return row, model


class TestBtc5mDecider:
    def setup_method(self):
        self.spot = MagicMock()
        self.book = MagicMock()
        self.repo = MagicMock()
        self.repo.get_trades_for_market.return_value = []

    def test_no_trade_when_edge_below_threshold(self):
        now = datetime.now(timezone.utc)
        row, _ = _make_market(now)
        self.spot.current.return_value = Decimal("65000")
        self.spot.realized_vol.return_value = 0.40
        self.book.fetch_mid.return_value = (Decimal("0.50"), Decimal("0.51"))

        decider = Btc5mDecider(
            spot=self.spot, book=self.book, repo=self.repo,
            edge_threshold=0.05, position_size_usd=Decimal("5"),
            fees_bps=0.0,
        )
        decider.evaluate(row)
        self.repo.insert_trade.assert_not_called()

    def test_trade_yes_when_estimator_above_market(self):
        now = datetime.now(timezone.utc)
        row, _ = _make_market(now, ttm_s=60)
        self.spot.current.return_value = Decimal("65500")
        self.spot.realized_vol.return_value = 0.10
        self.book.fetch_mid.return_value = (Decimal("0.80"), Decimal("0.82"))
        decider = Btc5mDecider(
            spot=self.spot, book=self.book, repo=self.repo,
            edge_threshold=0.05, position_size_usd=Decimal("5"),
            fees_bps=0.0,
        )
        row["start_spot"] = Decimal("65000")
        decider.evaluate(row)
        self.repo.insert_trade.assert_called_once()
        trade = self.repo.insert_trade.call_args[0][0]
        assert trade.side == "YES"
        assert trade.fill_price_assumed == Decimal("0.82")

    def test_trade_no_when_estimator_below_market(self):
        now = datetime.now(timezone.utc)
        row, _ = _make_market(now, ttm_s=60)
        row["start_spot"] = Decimal("65000")
        self.spot.current.return_value = Decimal("64500")
        self.spot.realized_vol.return_value = 0.10
        self.book.fetch_mid.return_value = (Decimal("0.80"), Decimal("0.82"))

        decider = Btc5mDecider(
            spot=self.spot, book=self.book, repo=self.repo,
            edge_threshold=0.05, position_size_usd=Decimal("5"),
            fees_bps=0.0,
        )
        decider.evaluate(row)
        self.repo.insert_trade.assert_called_once()
        trade = self.repo.insert_trade.call_args[0][0]
        assert trade.side == "NO"
        assert trade.fill_price_assumed == Decimal("0.80")

    def test_skips_if_already_traded_on_market(self):
        now = datetime.now(timezone.utc)
        row, _ = _make_market(now, ttm_s=60)
        row["start_spot"] = Decimal("65000")
        self.repo.get_trades_for_market.return_value = [{"id": uuid4()}]
        self.spot.current.return_value = Decimal("65500")
        self.spot.realized_vol.return_value = 0.10
        self.book.fetch_mid.return_value = (Decimal("0.30"), Decimal("0.32"))

        decider = Btc5mDecider(
            spot=self.spot, book=self.book, repo=self.repo,
            edge_threshold=0.05, position_size_usd=Decimal("5"),
            fees_bps=0.0,
        )
        decider.evaluate(row)
        self.repo.insert_trade.assert_not_called()

    def test_skips_if_no_spot(self):
        now = datetime.now(timezone.utc)
        row, _ = _make_market(now, ttm_s=60)
        self.spot.current.return_value = None

        decider = Btc5mDecider(
            spot=self.spot, book=self.book, repo=self.repo,
            edge_threshold=0.05, position_size_usd=Decimal("5"),
            fees_bps=0.0,
        )
        decider.evaluate(row)
        self.repo.insert_trade.assert_not_called()

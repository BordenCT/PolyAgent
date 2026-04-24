"""Tests for Btc5mRepository."""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

from polyagent.data.repositories.btc5m import Btc5mRepository
from polyagent.models import Btc5mMarket, Btc5mTrade


class TestBtc5mRepository:
    def setup_method(self):
        self.db = MagicMock()
        self.repo = Btc5mRepository(self.db)

    def _mock_cursor(self, fetchone=None, fetchall=None):
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        if fetchone is not None:
            cur.fetchone.return_value = fetchone
        if fetchall is not None:
            cur.fetchall.return_value = fetchall
        self.db.cursor.return_value = cur
        return cur

    def test_upsert_market_returns_id(self):
        expected = uuid4()
        self._mock_cursor(fetchone={"id": expected})
        market = Btc5mMarket(
            polymarket_id="0x1",
            slug="btc-updown-5m-1776995400",
            token_id_yes="y",
            token_id_no="n",
            window_duration_s=300,
            window_start_ts=datetime(2026, 4, 24, 1, 45, tzinfo=timezone.utc),
            window_end_ts=datetime(2026, 4, 24, 1, 50, tzinfo=timezone.utc),
        )
        result = self.repo.upsert_market(market)
        assert result == expected

    def test_insert_trade_returns_id(self):
        expected = uuid4()
        self._mock_cursor(fetchone={"id": expected})
        trade = Btc5mTrade(
            market_id=uuid4(),
            side="YES",
            fill_price_assumed=Decimal("0.52"),
            size=Decimal("5.00"),
            estimator_p_up=0.58,
            spot_at_decision=Decimal("65000"),
            vol_at_decision=0.45,
            edge_at_decision=0.06,
        )
        result = self.repo.insert_trade(trade)
        assert result == expected

    def test_get_unresolved_markets_returns_list(self):
        now = datetime.now(timezone.utc)
        self._mock_cursor(fetchall=[
            {"id": uuid4(), "polymarket_id": "0x1", "slug": "s", "token_id_yes": "y",
             "token_id_no": "n", "window_duration_s": 300,
             "window_start_ts": now, "window_end_ts": now,
             "start_spot": None, "end_spot": None, "outcome": None}
        ])
        rows = self.repo.get_unresolved_markets_past_end(now)
        assert len(rows) == 1

    def test_update_market_resolution(self):
        self._mock_cursor(fetchone=None)
        self.repo.update_market_resolution(
            uuid4(),
            start_spot=Decimal("65000"),
            end_spot=Decimal("65100"),
            outcome="YES",
        )

    def test_update_trade_pnl(self):
        self._mock_cursor(fetchone=None)
        self.repo.update_trade_pnl(uuid4(), Decimal("0.50"))

"""Tests for market repository."""
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

from polyagent.data.repositories.markets import MarketRepository
from polyagent.models import MarketData, MarketStatus


class TestMarketRepository:
    def setup_method(self):
        self.db = MagicMock()
        self.repo = MarketRepository(self.db)

    def test_upsert_market_executes_query(self):
        market = MarketData(
            polymarket_id="0xabc",
            question="Will BTC hit 150k?",
            category="crypto",
            token_id="tok_1",
            midpoint_price=Decimal("0.45"),
            bids_depth=Decimal("2000"),
            asks_depth=Decimal("1800"),
            hours_to_resolution=48.0,
            volume_24h=Decimal("150000"),
        )
        self.repo.upsert(market)
        self.db.cursor.assert_called_once()

    def test_get_by_status_returns_list(self):
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall.return_value = [
            {
                "id": uuid4(),
                "polymarket_id": "0x1",
                "question": "test?",
                "category": "crypto",
                "token_id": "t1",
                "midpoint_price": Decimal("0.5"),
                "bids_depth": Decimal("1000"),
                "asks_depth": Decimal("900"),
                "hours_to_resolution": 24.0,
                "volume_24h": Decimal("50000"),
                "status": "queued",
            }
        ]
        self.db.cursor.return_value = mock_cursor
        results = self.repo.get_by_status(MarketStatus.QUEUED)
        assert len(results) == 1
        assert results[0]["polymarket_id"] == "0x1"

    def test_upsert_passes_market_class_to_sql(self):
        from polyagent.models import MarketClass

        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = {"id": uuid4()}
        self.db.cursor.return_value = mock_cursor

        market = MarketData(
            polymarket_id="0xabc",
            question="Will BTC hit 150k?",
            category="crypto",
            token_id="tok_1",
            midpoint_price=Decimal("0.45"),
            bids_depth=Decimal("2000"),
            asks_depth=Decimal("1800"),
            hours_to_resolution=48.0,
            volume_24h=Decimal("150000"),
            market_class=MarketClass.CRYPTO,
        )
        self.repo.upsert(market)

        args, kwargs = mock_cursor.execute.call_args
        # psycopg call shape: execute(sql, params_dict)
        params = args[1] if len(args) > 1 else kwargs.get("params") or kwargs
        assert params["market_class"] == "crypto"

    def test_upsert_defaults_market_class_when_unset(self):
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = {"id": uuid4()}
        self.db.cursor.return_value = mock_cursor

        market = MarketData(
            polymarket_id="0xabc",
            question="q",
            category="unknown",
            token_id="tok",
            midpoint_price=Decimal("0.5"),
            bids_depth=Decimal("100"),
            asks_depth=Decimal("100"),
            hours_to_resolution=10.0,
            volume_24h=Decimal("0"),
            # market_class omitted
        )
        self.repo.upsert(market)

        args, kwargs = mock_cursor.execute.call_args
        params = args[1] if len(args) > 1 else kwargs.get("params") or kwargs
        assert params["market_class"] == "other"

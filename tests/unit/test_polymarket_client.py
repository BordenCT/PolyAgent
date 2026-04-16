"""Tests for Polymarket CLOB client."""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from polyagent.data.clients.polymarket import PolymarketClient
from polyagent.models import MarketData


class TestPolymarketClient:
    def setup_method(self):
        self.client = PolymarketClient(base_url="https://clob.polymarket.com")

    def test_parse_market_response(self):
        raw = {
            "condition_id": "0xabc123",
            "question": "Will BTC exceed $150k by July 2026?",
            "tokens": [{"token_id": "tok_yes", "outcome": "Yes"}],
            "category": "crypto",
            "end_date_iso": "2026-07-01T00:00:00Z",
            "best_bid": 0.42,
            "best_ask": 0.48,
            "volume": 150000.0,
        }
        market = self.client.parse_market(raw)
        assert market.polymarket_id == "0xabc123"
        assert market.question == "Will BTC exceed $150k by July 2026?"
        assert market.token_id == "tok_yes"
        assert market.category == "crypto"
        assert isinstance(market.midpoint_price, Decimal)

    def test_parse_market_calculates_midpoint(self):
        raw = {
            "condition_id": "0x1",
            "question": "test?",
            "tokens": [{"token_id": "t1", "outcome": "Yes"}],
            "category": "politics",
            "end_date_iso": "2026-07-01T00:00:00Z",
            "best_bid": 0.40,
            "best_ask": 0.60,
            "volume": 50000.0,
        }
        market = self.client.parse_market(raw)
        assert market.midpoint_price == Decimal("0.5")

    def test_parse_market_missing_tokens_skips(self):
        raw = {
            "condition_id": "0x2",
            "question": "test?",
            "tokens": [],
            "category": "crypto",
            "end_date_iso": "2026-07-01T00:00:00Z",
            "best_bid": 0.4,
            "best_ask": 0.6,
            "volume": 100.0,
        }
        result = self.client.parse_market(raw)
        assert result is None

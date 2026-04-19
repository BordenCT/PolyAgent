"""Tests for Polymarket Gamma/CLOB client."""
import json
from decimal import Decimal

from polyagent.data.clients.polymarket import PolymarketClient
from polyagent.models import MarketData

_GAMMA_RAW = {
    "conditionId": "0xabc123",
    "question": "Will BTC exceed $150k by July 2026?",
    "clobTokenIds": json.dumps(["tok_yes", "tok_no"]),
    "outcomePrices": json.dumps(["0.45", "0.55"]),
    "category": "crypto",
    "endDate": "2026-07-01T00:00:00Z",
    "liquidityNum": 250000.0,
    "volume24hr": 150000.0,
}


class TestPolymarketClient:
    def setup_method(self):
        self.client = PolymarketClient(base_url="https://clob.polymarket.com")

    def test_parse_market_response(self):
        market = self.client.parse_market(_GAMMA_RAW)
        assert market is not None
        assert market.polymarket_id == "0xabc123"
        assert market.question == "Will BTC exceed $150k by July 2026?"
        assert market.token_id == "tok_yes"
        assert market.category == "crypto"
        assert isinstance(market.midpoint_price, Decimal)

    def test_parse_market_uses_yes_price(self):
        raw = dict(_GAMMA_RAW, outcomePrices=json.dumps(["0.40", "0.60"]))
        market = self.client.parse_market(raw)
        assert market is not None
        assert market.midpoint_price == Decimal("0.4")

    def test_parse_market_uses_liquidity_as_depth(self):
        market = self.client.parse_market(_GAMMA_RAW)
        assert market is not None
        assert market.bids_depth == Decimal("250000.0")
        assert market.asks_depth == Decimal("250000.0")
        assert market.min_depth == Decimal("250000.0")

    def test_parse_market_missing_condition_id_skips(self):
        raw = dict(_GAMMA_RAW)
        del raw["conditionId"]
        assert self.client.parse_market(raw) is None

    def test_parse_market_missing_tokens_skips(self):
        raw = dict(_GAMMA_RAW, clobTokenIds=json.dumps([]))
        assert self.client.parse_market(raw) is None

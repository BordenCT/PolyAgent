"""Tests for Polymarket Gamma/CLOB client."""
import json
from decimal import Decimal
from unittest.mock import MagicMock

from polyagent.data.clients.polymarket import PolymarketClient
from polyagent.models import MarketData


def _clob_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp

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

    def test_parse_market_uses_volume24h_as_depth(self):
        market = self.client.parse_market(_GAMMA_RAW)
        assert market is not None
        assert market.bids_depth == Decimal("150000.0")
        assert market.asks_depth == Decimal("150000.0")
        assert market.min_depth == Decimal("150000.0")

    def test_parse_market_missing_condition_id_skips(self):
        raw = dict(_GAMMA_RAW)
        del raw["conditionId"]
        assert self.client.parse_market(raw) is None

    def test_parse_market_missing_tokens_skips(self):
        raw = dict(_GAMMA_RAW, clobTokenIds=json.dumps([]))
        assert self.client.parse_market(raw) is None


class TestFetchMarketState:
    def setup_method(self):
        self.client = PolymarketClient(base_url="https://clob.polymarket.com")

    def test_resolved_yes_pins_current_price_to_one(self):
        payload = {
            "closed": True,
            "best_bid": 0,
            "best_ask": 0,
            "volume": 1000,
            "tokens": [
                {"token_id": "tok_yes", "outcome": "Yes", "winner": True},
                {"token_id": "tok_no", "outcome": "No", "winner": False},
            ],
        }
        self.client._http.get = MagicMock(return_value=_clob_response(payload))
        state = self.client.fetch_market_state("0xabc")
        assert state is not None
        assert state["is_resolved"] is True
        assert state["midpoint_price"] == Decimal("1")

    def test_resolved_no_pins_current_price_to_zero(self):
        payload = {
            "closed": True,
            "best_bid": 0,
            "best_ask": 0,
            "volume": 1000,
            "tokens": [
                {"token_id": "tok_yes", "outcome": "Yes", "winner": False},
                {"token_id": "tok_no", "outcome": "No", "winner": True},
            ],
        }
        self.client._http.get = MagicMock(return_value=_clob_response(payload))
        state = self.client.fetch_market_state("0xabc")
        assert state is not None
        assert state["is_resolved"] is True
        assert state["midpoint_price"] == Decimal("0")

    def test_closed_without_winner_is_not_resolved(self):
        """`closed=true` can fire for paused or delisted markets — only a
        winner flag on a token means the market has actually resolved."""
        payload = {
            "closed": True,
            "archived": False,
            "best_bid": 0.42,
            "best_ask": 0.46,
            "volume": 1000,
            "tokens": [
                {"token_id": "tok_yes", "outcome": "Yes", "winner": False},
                {"token_id": "tok_no", "outcome": "No", "winner": False},
            ],
        }
        self.client._http.get = MagicMock(return_value=_clob_response(payload))
        state = self.client.fetch_market_state("0xabc")
        assert state is not None
        assert state["is_resolved"] is False
        assert state["midpoint_price"] == Decimal("0.44")

    def test_live_market_uses_book_midpoint(self):
        payload = {
            "closed": False,
            "best_bid": 0.50,
            "best_ask": 0.54,
            "volume": 2000,
            "tokens": [
                {"token_id": "tok_yes", "outcome": "Yes", "winner": False},
                {"token_id": "tok_no", "outcome": "No", "winner": False},
            ],
        }
        self.client._http.get = MagicMock(return_value=_clob_response(payload))
        state = self.client.fetch_market_state("0xabc")
        assert state is not None
        assert state["is_resolved"] is False
        assert state["midpoint_price"] == Decimal("0.52")

    def test_missing_token_ids_do_not_classify_as_yes_won(self):
        """Defensive: if both YES and winner token are present but token_id
        is absent on both, None == None must not silently flip to YES-won."""
        payload = {
            "closed": True,
            "best_bid": 0,
            "best_ask": 0,
            "tokens": [
                {"outcome": "Yes", "winner": False},
                {"outcome": "No", "winner": True},
            ],
        }
        self.client._http.get = MagicMock(return_value=_clob_response(payload))
        state = self.client.fetch_market_state("0xabc")
        assert state is not None
        assert state["is_resolved"] is True
        assert state["midpoint_price"] == Decimal("0")

    def test_categorical_market_without_yes_token_defaults_to_zero(self):
        payload = {
            "closed": True,
            "best_bid": 0,
            "best_ask": 0,
            "tokens": [
                {"token_id": "tok_a", "outcome": "Trump", "winner": True},
                {"token_id": "tok_b", "outcome": "Harris", "winner": False},
            ],
        }
        self.client._http.get = MagicMock(return_value=_clob_response(payload))
        state = self.client.fetch_market_state("0xabc")
        assert state is not None
        assert state["is_resolved"] is True
        assert state["midpoint_price"] == Decimal("0")

"""Tests for domain models."""
from decimal import Decimal
from datetime import datetime, timezone
from uuid import uuid4

from polyagent.models import (
    ExitReason, MarketClass, MarketData, MarketStatus, PositionSide, PositionStatus,
    Score, ThesisChecks, Vote, VoteAction,
)


class TestScore:
    def test_create_score(self):
        score = Score(gap=0.12, depth=1500.0, hours=24.0, ev=0.18)
        assert score.gap == 0.12
        assert score.ev == 0.18

    def test_score_immutable(self):
        score = Score(gap=0.12, depth=1500.0, hours=24.0, ev=0.18)
        try:
            score.gap = 0.5
            assert False, "Should be frozen"
        except AttributeError:
            pass


class TestMarketData:
    def test_create_market(self):
        m = MarketData(
            polymarket_id="0x123", question="Will BTC hit 150k?",
            category="crypto", token_id="tok_abc",
            midpoint_price=Decimal("0.45"), bids_depth=Decimal("2000"),
            asks_depth=Decimal("1800"), hours_to_resolution=48.0,
            volume_24h=Decimal("150000"),
        )
        assert m.polymarket_id == "0x123"
        assert m.min_depth == Decimal("1800")

    def test_min_depth_returns_smaller_side(self):
        m = MarketData(
            polymarket_id="0x1", question="test", category="test",
            token_id="t1", midpoint_price=Decimal("0.5"),
            bids_depth=Decimal("500"), asks_depth=Decimal("800"),
            hours_to_resolution=10.0, volume_24h=Decimal("50000"),
        )
        assert m.min_depth == Decimal("500")


class TestVote:
    def test_buy_vote(self):
        v = Vote(action=VoteAction.BUY, confidence=0.82, reason="Strong convergence signal")
        assert v.action == VoteAction.BUY

    def test_hold_vote(self):
        v = Vote(action=VoteAction.HOLD, confidence=0.4, reason="Weak signal")
        assert v.action == VoteAction.HOLD


class TestThesisChecks:
    def test_count_passed(self):
        checks = ThesisChecks(base_rate=True, news=True, whale=False, disposition=True)
        assert checks.passed_count == 3

    def test_all_failed(self):
        checks = ThesisChecks(base_rate=False, news=False, whale=False, disposition=False)
        assert checks.passed_count == 0


class TestMarketClass:
    def test_values(self):
        assert MarketClass.SPORTS.value == "sports"
        assert MarketClass.CRYPTO.value == "crypto"
        assert MarketClass.POLITICS.value == "politics"
        assert MarketClass.MACRO.value == "macro"
        assert MarketClass.OTHER.value == "other"

    def test_str_enum_behavior(self):
        assert MarketClass.SPORTS == "sports"

    def test_market_data_default_class_is_none(self):
        m = MarketData(
            polymarket_id="0x1", question="q", category="c",
            token_id="t", midpoint_price=Decimal("0.5"),
            bids_depth=Decimal("1"), asks_depth=Decimal("1"),
            hours_to_resolution=1.0, volume_24h=Decimal("1"),
        )
        assert m.market_class is None

    def test_market_data_accepts_class(self):
        m = MarketData(
            polymarket_id="0x1", question="q", category="c",
            token_id="t", midpoint_price=Decimal("0.5"),
            bids_depth=Decimal("1"), asks_depth=Decimal("1"),
            hours_to_resolution=1.0, volume_24h=Decimal("1"),
            market_class=MarketClass.CRYPTO,
        )
        assert m.market_class == MarketClass.CRYPTO


from polyagent.models import QuantShortMarket, QuantShortTrade


class TestQuantShortMarket:
    def test_open_market_has_no_outcome(self):
        m = QuantShortMarket(
            polymarket_id="0xabc",
            slug="btc-updown-5m-1776995400",
            token_id_yes="y",
            token_id_no="n",
            window_duration_s=300,
            window_start_ts=datetime(2026, 4, 24, 1, 45, tzinfo=timezone.utc),
            window_end_ts=datetime(2026, 4, 24, 1, 50, tzinfo=timezone.utc),
            asset_id="BTC",
        )
        assert m.outcome is None
        assert m.start_spot is None
        assert m.end_spot is None
        assert m.window_duration_s == 300
        assert m.asset_id == "BTC"

    def test_resolved_15m_market_has_outcome_and_spots(self):
        m = QuantShortMarket(
            polymarket_id="0xabc",
            slug="btc-updown-15m-1776995400",
            token_id_yes="y",
            token_id_no="n",
            window_duration_s=900,
            window_start_ts=datetime(2026, 4, 24, 1, 35, tzinfo=timezone.utc),
            window_end_ts=datetime(2026, 4, 24, 1, 50, tzinfo=timezone.utc),
            asset_id="BTC",
            start_spot=Decimal("65000"),
            end_spot=Decimal("65100"),
            outcome="YES",
        )
        assert m.outcome == "YES"
        assert m.window_duration_s == 900


class TestQuantShortTrade:
    def test_create_unresolved_trade(self):
        t = QuantShortTrade(
            market_id=uuid4(),
            side="YES",
            fill_price_assumed=Decimal("0.52"),
            size=Decimal("5.00"),
            estimator_p_up=0.58,
            spot_at_decision=Decimal("65000"),
            vol_at_decision=0.45,
            edge_at_decision=0.06,
        )
        assert t.pnl is None

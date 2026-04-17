"""Tests for the scanner service."""
from decimal import Decimal

import pytest

from polyagent.models import MarketData, Score
from polyagent.services.scanner import ScannerService


class TestScoreMarket:
    def setup_method(self):
        self.scanner = ScannerService(
            min_gap=0.07,
            min_depth=500.0,
            min_hours=4.0,
            max_hours=168.0,
        )

    def _make_market(self, **overrides) -> MarketData:
        defaults = {
            "polymarket_id": "0x1",
            "question": "test?",
            "category": "crypto",
            "token_id": "t1",
            "midpoint_price": Decimal("0.40"),
            "bids_depth": Decimal("2000"),
            "asks_depth": Decimal("1800"),
            "hours_to_resolution": 48.0,
            "volume_24h": Decimal("100000"),
        }
        defaults.update(overrides)
        return MarketData(**defaults)

    def test_good_market_scores(self):
        market = self._make_market()
        historical_estimate = 0.55  # gap = |0.55 - 0.40| = 0.15
        score = self.scanner.score_market(market, historical_estimate)
        assert score is not None
        assert score.gap == 0.15
        assert score.depth == 1800.0
        assert score.hours == 48.0
        assert score.ev == round(0.15 * 1800 * 0.001, 2)

    def test_gap_too_thin_rejected(self):
        market = self._make_market(midpoint_price=Decimal("0.50"))
        historical_estimate = 0.53  # gap = 0.03 < 0.07
        score = self.scanner.score_market(market, historical_estimate)
        assert score is None

    def test_depth_too_shallow_rejected(self):
        market = self._make_market(bids_depth=Decimal("200"), asks_depth=Decimal("300"))
        historical_estimate = 0.55
        score = self.scanner.score_market(market, historical_estimate)
        assert score is None

    def test_too_late_rejected(self):
        market = self._make_market(hours_to_resolution=2.0)
        historical_estimate = 0.55
        score = self.scanner.score_market(market, historical_estimate)
        assert score is None

    def test_too_slow_rejected(self):
        market = self._make_market(hours_to_resolution=200.0)
        historical_estimate = 0.55
        score = self.scanner.score_market(market, historical_estimate)
        assert score is None

    def test_exact_threshold_gap_rejected(self):
        market = self._make_market(midpoint_price=Decimal("0.50"))
        historical_estimate = 0.57  # gap = 0.07, not strictly >
        score = self.scanner.score_market(market, historical_estimate)
        assert score is None

    def test_just_above_threshold_gap_passes(self):
        market = self._make_market(midpoint_price=Decimal("0.50"))
        historical_estimate = 0.58  # gap = 0.08 > 0.07
        score = self.scanner.score_market(market, historical_estimate)
        assert score is not None
        assert score.gap == pytest.approx(0.08, abs=0.001)

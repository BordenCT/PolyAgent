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

    def test_default_blocklist_kills_btc_dip(self):
        # Barrier-touch markets ("dip to" / "reach") are still blocked — the
        # quant pipeline handles strikes/ranges but not barrier-touch math yet.
        market = self._make_market(question="Will Bitcoin dip to $77,000 on April 25?")
        score = self.scanner.score_market(market, 0.60)
        assert score is None

    def test_default_blocklist_kills_btc_reach(self):
        market = self._make_market(question="Will Bitcoin reach $79,000 on April 24?")
        score = self.scanner.score_market(market, 0.60)
        assert score is None

    def test_default_blocklist_allows_strike_ladders(self):
        # Strike/range ladders are now routed to the crypto-quant brain path.
        # The scanner must let them through.
        market = self._make_market(question="Will the price of Bitcoin be above $80,000 on April 26?")
        score = self.scanner.score_market(market, 0.60)
        assert score is not None

    def test_blocklist_does_not_kill_sports(self):
        market = self._make_market(question="Madrid Open: Sinner vs Bonzi")
        score = self.scanner.score_market(market, 0.55)
        assert score is not None

    def test_custom_blocklist_overrides_default(self):
        scanner = ScannerService(
            min_gap=0.07, min_depth=500.0, min_hours=4.0, max_hours=168.0,
            question_blocklist=(r"^UFC ",),
        )
        # "dip to" no longer blocked under custom list:
        m1 = self._make_market(question="Will Bitcoin dip to $77,000 on April 25?")
        assert scanner.score_market(m1, 0.60) is not None
        # UFC now blocked:
        m2 = self._make_market(question="UFC Fight Night: Eric McConico vs. Rodolfo Vieira")
        assert scanner.score_market(m2, 0.60) is None

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

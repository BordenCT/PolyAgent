"""Tests for backtest probability estimators."""
from polyagent.backtest.estimator import (
    HistoricalEstimator,
    MidpointEstimator,
)


class TestHistoricalEstimator:
    def test_resolved_yes_returns_high(self):
        estimator = HistoricalEstimator()
        p = estimator.estimate("0x1", outcome="Yes", final_price=1.0)
        assert p == 1.0

    def test_resolved_no_returns_low(self):
        estimator = HistoricalEstimator()
        p = estimator.estimate("0x1", outcome="No", final_price=0.0)
        assert p == 0.0

    def test_partial_resolution(self):
        estimator = HistoricalEstimator()
        p = estimator.estimate("0x1", outcome="Yes", final_price=0.85)
        assert p == 0.85


class TestMidpointEstimator:
    def test_returns_market_price(self):
        estimator = MidpointEstimator()
        p = estimator.estimate("0x1", market_price=0.55)
        assert p == 0.55

    def test_midpoint_produces_no_edge(self):
        """Using market price as estimate should produce ~0 gap after scoring."""
        estimator = MidpointEstimator()
        price = 0.50
        estimate = estimator.estimate("0x1", market_price=price)
        gap = abs(estimate - price)
        assert gap == 0.0  # no edge, scanner should kill this

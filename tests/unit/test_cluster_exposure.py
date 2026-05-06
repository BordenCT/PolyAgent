"""Unit tests for the pure cluster-exposure helper.

The repository's `sum_open_size_by_cluster` method delegates filtering
and summation to `sum_positions_in_cluster`, which is testable without
a live database. Tests below exercise the cluster-matching logic on
synthesised position rows.
"""
from __future__ import annotations

from decimal import Decimal

from polyagent.services.cluster_exposure import sum_positions_in_cluster


def _row(question: str, size: str) -> dict:
    return {"question": question, "position_size": Decimal(size)}


def test_sums_only_matching_cluster():
    positions = [
        _row("Will the price of Bitcoin be above $80,000 on May 6?", "4.19"),
        _row("Will the price of Bitcoin be above $82,000 on May 6?", "2.68"),
        _row("Will the price of Ethereum be above $2,400 on May 6?", "3.96"),
    ]
    assert sum_positions_in_cluster(positions, ("BTC", "may 6")) == Decimal("6.87")


def test_returns_zero_when_no_match():
    positions = [
        _row("Will the price of Bitcoin be above $80,000 on May 6?", "4.19"),
    ]
    assert sum_positions_in_cluster(positions, ("ETH", "may 6")) == Decimal("0")


def test_ignores_unparseable_questions():
    """A non-strike open position contributes nothing to any cluster sum."""
    positions = [
        _row("Will Eagles win the Superbowl?", "5.00"),
        _row("Will the price of Bitcoin be above $80,000 on May 6?", "4.19"),
    ]
    assert sum_positions_in_cluster(positions, ("BTC", "may 6")) == Decimal("4.19")


def test_handles_case_variation_in_date():
    """Two positions on the same date with different casing collide."""
    positions = [
        _row("Will the price of Bitcoin be above $80,000 on May 6?", "4.19"),
        _row("Will the price of Bitcoin be below $82,000 on may 6?", "2.68"),
    ]
    assert sum_positions_in_cluster(positions, ("BTC", "may 6")) == Decimal("6.87")


def test_empty_positions_returns_zero():
    assert sum_positions_in_cluster([], ("BTC", "may 6")) == Decimal("0")

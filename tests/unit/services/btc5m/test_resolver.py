"""Tests for Btc5mResolver."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from polyagent.services.btc5m.resolver import Btc5mResolver


class TestPnl:
    @pytest.mark.parametrize("side,fill,outcome,size,expected", [
        # YES at 0.40, outcome YES: (1 - 0.40) * 5 = +3.00
        ("YES", Decimal("0.40"), "YES", Decimal("5"), Decimal("3.00")),
        # YES at 0.40, outcome NO: -0.40 * 5 = -2.00
        ("YES", Decimal("0.40"), "NO",  Decimal("5"), Decimal("-2.00")),
        # NO at 0.40, outcome NO: (1 - 0.40) * 5 = +3.00
        ("NO",  Decimal("0.40"), "NO",  Decimal("5"), Decimal("3.00")),
        # NO at 0.40, outcome YES: -0.40 * 5 = -2.00
        ("NO",  Decimal("0.40"), "YES", Decimal("5"), Decimal("-2.00")),
    ])
    def test_compute_pnl_cases(self, side, fill, outcome, size, expected):
        from polyagent.services.btc5m.resolver import compute_pnl
        assert compute_pnl(side, fill, outcome, size) == expected


class TestBtc5mResolver:
    def setup_method(self):
        self.repo = MagicMock()
        self.spot_history = MagicMock()
        self.resolver = Btc5mResolver(repo=self.repo, spot_history=self.spot_history)

    def test_resolves_market_and_updates_trade_pnl(self):
        mid = uuid4()
        tid = uuid4()
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(minutes=6)
        window_end = now - timedelta(minutes=1)
        self.repo.get_unresolved_markets_past_end.return_value = [{
            "id": mid, "polymarket_id": "0x1", "slug": "s",
            "window_start_ts": window_start, "window_end_ts": window_end,
        }]
        self.spot_history.price_at.side_effect = [
            Decimal("65000"),   # start
            Decimal("65100"),   # end → UP
        ]
        self.repo.get_trades_for_market.return_value = [{
            "id": tid, "side": "YES", "fill_price_assumed": Decimal("0.40"),
            "size": Decimal("5"), "pnl": None,
        }]

        self.resolver.resolve_due_markets()

        self.repo.update_market_resolution.assert_called_once_with(
            mid, start_spot=Decimal("65000"), end_spot=Decimal("65100"), outcome="YES"
        )
        self.repo.update_trade_pnl.assert_called_once_with(tid, Decimal("3.00"))

    def test_skips_market_if_spot_unavailable(self):
        mid = uuid4()
        now = datetime.now(timezone.utc)
        self.repo.get_unresolved_markets_past_end.return_value = [{
            "id": mid, "polymarket_id": "0x1", "slug": "s",
            "window_start_ts": now - timedelta(minutes=6),
            "window_end_ts": now - timedelta(minutes=1),
        }]
        self.spot_history.price_at.return_value = None

        self.resolver.resolve_due_markets()

        self.repo.update_market_resolution.assert_not_called()

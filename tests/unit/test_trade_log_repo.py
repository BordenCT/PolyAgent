"""Tests for trade log repository."""
from unittest.mock import MagicMock
from uuid import uuid4

from polyagent.data.repositories.trade_log import TradeLogRepository


class TestTradeLogRepository:
    def setup_method(self):
        self.db = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        self.cursor = cursor
        self.db.cursor.return_value = cursor
        self.repo = TradeLogRepository(self.db)

    def test_insert_minimal(self):
        pos_id = uuid4()
        self.repo.insert(position_id=pos_id, action="OPEN_PAPER")
        args, _ = self.cursor.execute.call_args
        params = args[1]
        assert params["position_id"] == pos_id
        assert params["action"] == "OPEN_PAPER"
        assert params["reason"] is None
        assert params["raw_request"] is None
        assert params["raw_response"] is None
        assert params["error"] is None
        assert params["logged_at"] is not None

    def test_insert_serializes_json_payloads(self):
        self.repo.insert(
            position_id=uuid4(),
            action="OPEN_LIVE",
            reason="full",
            raw_request={"token_id": "t1", "price": 0.5},
            raw_response={"order_id": "abc"},
        )
        args, _ = self.cursor.execute.call_args
        params = args[1]
        assert "token_id" in params["raw_request"]
        assert "order_id" in params["raw_response"]

    def test_insert_captures_error(self):
        self.repo.insert(
            position_id=uuid4(),
            action="OPEN_LIVE_FAILED",
            error="CLI exit code 1",
        )
        args, _ = self.cursor.execute.call_args
        assert args[1]["error"] == "CLI exit code 1"

    def test_get_by_position_returns_rows(self):
        target_id = uuid4()
        self.cursor.fetchall.return_value = [{"action": "OPEN_PAPER"}]
        rows = self.repo.get_by_position(target_id)
        assert len(rows) == 1
        args, _ = self.cursor.execute.call_args
        assert args[1]["position_id"] == target_id

"""Tests for backtest repository."""
from datetime import date, datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

from polyagent.data.repositories.backtests import BacktestRepository


class TestBacktestRepository:
    def setup_method(self):
        self.db = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        self.cursor = mock_cursor
        self.db.cursor.return_value = mock_cursor
        self.repo = BacktestRepository(self.db)

    def test_create_run_returns_uuid(self):
        run_id = self.repo.create_run(
            date_start=date(2025, 1, 1),
            date_end=date(2025, 6, 30),
            estimator="historical",
            parameters={"bankroll": 800.0},
        )
        assert run_id is not None
        self.cursor.execute.assert_called_once()
        args, _ = self.cursor.execute.call_args
        params = args[1]
        assert params["id"] == run_id
        assert params["estimator"] == "historical"
        assert params["date_start"] == date(2025, 1, 1)

    def test_complete_run_updates_aggregates(self):
        run_id = uuid4()
        self.repo.complete_run(
            run_id=run_id,
            total_trades=10,
            win_rate=60.0,
            total_pnl=150.0,
            sharpe=1.2,
            max_drawdown=8.5,
            results={"avg_pnl": 15.0, "winners": 6, "losers": 4},
        )
        args, _ = self.cursor.execute.call_args
        params = args[1]
        assert params["id"] == run_id
        assert params["total_trades"] == 10
        assert params["win_rate"] == 60.0
        assert "avg_pnl" in params["results"]  # JSON string

    def test_insert_positions_skips_empty(self):
        self.repo.insert_positions(uuid4(), [])
        self.cursor.executemany.assert_not_called()

    def test_insert_positions_bulk_inserts(self):
        run_id = uuid4()
        trades = [
            {
                "polymarket_id": "0x1",
                "question": "Will X happen?",
                "category": "crypto",
                "side": "BUY",
                "entry_price": 0.4,
                "exit_price": 1.0,
                "target_price": 0.85,
                "kelly_fraction": 0.1,
                "position_size": 80.0,
                "pnl": 120.0,
                "exit_reason": "TARGET_HIT",
                "entry_date": "2025-06-15",
                "exit_date": None,
                "estimator_prob": 0.9,
                "market_price": 0.4,
            },
        ]
        self.repo.insert_positions(run_id, trades)
        self.cursor.executemany.assert_called_once()
        _, kwargs = self.cursor.executemany.call_args
        args, _ = self.cursor.executemany.call_args
        rows = args[1]
        assert len(rows) == 1
        assert rows[0]["run_id"] == run_id
        assert rows[0]["polymarket_id"] == "0x1"

    def test_insert_positions_defaults_side_and_target(self):
        run_id = uuid4()
        trades = [
            {
                "polymarket_id": "0x1",
                "question": "?",
                "category": "crypto",
                "entry_price": 0.4,
                "exit_price": 1.0,
                "kelly_fraction": 0.1,
                "position_size": 80.0,
                "pnl": 60.0,
                "exit_reason": "TARGET_HIT",
                "entry_date": "2025-06-15",
                "estimator_prob": 0.9,
                "market_price": 0.4,
            },
        ]
        self.repo.insert_positions(run_id, trades)
        args, _ = self.cursor.executemany.call_args
        rows = args[1]
        assert rows[0]["side"] == "BUY"
        assert rows[0]["target_price"] == 0.4  # defaults to entry_price

    def test_get_latest_returns_row(self):
        self.cursor.fetchone.return_value = {
            "id": uuid4(),
            "estimator": "historical",
            "completed_at": datetime.now(timezone.utc),
        }
        result = self.repo.get_latest()
        assert result is not None
        assert result["estimator"] == "historical"

    def test_get_latest_returns_none_when_empty(self):
        self.cursor.fetchone.return_value = None
        assert self.repo.get_latest() is None

    def test_get_by_id_fetches_row(self):
        target_id = uuid4()
        self.cursor.fetchone.return_value = {"id": target_id, "estimator": "midpoint"}
        result = self.repo.get_by_id(target_id)
        assert result["id"] == target_id
        args, _ = self.cursor.execute.call_args
        assert args[1]["id"] == target_id

    def test_get_positions_returns_list(self):
        run_id = uuid4()
        self.cursor.fetchall.return_value = [{"polymarket_id": "0x1"}]
        results = self.repo.get_positions(run_id)
        assert len(results) == 1
        args, _ = self.cursor.execute.call_args
        assert args[1]["run_id"] == run_id

    def test_list_runs_applies_limit(self):
        self.cursor.fetchall.return_value = []
        self.repo.list_runs(limit=5)
        args, _ = self.cursor.execute.call_args
        assert args[1]["limit"] == 5

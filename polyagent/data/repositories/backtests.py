"""Backtest repository — persists backtest runs and per-trade positions."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from polyagent.infra.database import Database

logger = logging.getLogger("polyagent.repositories.backtests")

INSERT_RUN = """
    INSERT INTO backtest_runs (
        id, started_at, date_start, date_end, estimator, parameters
    ) VALUES (
        %(id)s, %(started_at)s, %(date_start)s, %(date_end)s,
        %(estimator)s, %(parameters)s
    )
"""

COMPLETE_RUN = """
    UPDATE backtest_runs
    SET completed_at = %(completed_at)s,
        results = %(results)s,
        total_trades = %(total_trades)s,
        win_rate = %(win_rate)s,
        total_pnl = %(total_pnl)s,
        sharpe = %(sharpe)s,
        max_drawdown = %(max_drawdown)s
    WHERE id = %(id)s
"""

INSERT_POSITION = """
    INSERT INTO backtest_positions (
        run_id, polymarket_id, question, category, side,
        entry_price, exit_price, target_price, kelly_fraction,
        position_size, pnl, exit_reason, entry_date, exit_date,
        estimator_prob, market_price
    ) VALUES (
        %(run_id)s, %(polymarket_id)s, %(question)s, %(category)s, %(side)s,
        %(entry_price)s, %(exit_price)s, %(target_price)s, %(kelly_fraction)s,
        %(position_size)s, %(pnl)s, %(exit_reason)s, %(entry_date)s,
        %(exit_date)s, %(estimator_prob)s, %(market_price)s
    )
"""

SELECT_LATEST = """
    SELECT * FROM backtest_runs
    WHERE completed_at IS NOT NULL
    ORDER BY completed_at DESC
    LIMIT 1
"""

SELECT_BY_ID = """
    SELECT * FROM backtest_runs WHERE id = %(id)s
"""

SELECT_POSITIONS_BY_RUN = """
    SELECT * FROM backtest_positions
    WHERE run_id = %(run_id)s
    ORDER BY entry_date, polymarket_id
"""

LIST_RUNS = """
    SELECT id, started_at, completed_at, date_start, date_end,
           estimator, total_trades, win_rate, total_pnl,
           sharpe, max_drawdown
    FROM backtest_runs
    ORDER BY started_at DESC
    LIMIT %(limit)s
"""


class BacktestRepository:
    """Persistence for backtest runs and their per-trade positions."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def create_run(
        self,
        date_start: date,
        date_end: date,
        estimator: str,
        parameters: dict[str, Any],
    ) -> UUID:
        """Create a new run row in the 'started' state and return its UUID.

        Args:
            date_start: Inclusive start date of the backtest window.
            date_end: Inclusive end date of the backtest window.
            estimator: Name of the estimator being tested.
            parameters: Input parameters (bankroll, kelly_max, etc.) as JSON-safe dict.

        Returns:
            The UUID of the newly inserted row.
        """
        run_id = uuid4()
        with self._db.cursor() as cur:
            cur.execute(
                INSERT_RUN,
                {
                    "id": run_id,
                    "started_at": datetime.now(timezone.utc),
                    "date_start": date_start,
                    "date_end": date_end,
                    "estimator": estimator,
                    "parameters": json.dumps(parameters),
                },
            )
        return run_id

    def complete_run(
        self,
        run_id: UUID,
        total_trades: int,
        win_rate: float,
        total_pnl: float,
        sharpe: float,
        max_drawdown: float,
        results: dict[str, Any],
    ) -> None:
        """Mark a run as complete and store its aggregate metrics.

        Args:
            run_id: The UUID returned by create_run.
            total_trades: Count of trades taken during the run.
            win_rate: Win rate as a percentage (0-100).
            total_pnl: Total realized P&L in USD.
            sharpe: Sharpe ratio across the trade sequence.
            max_drawdown: Maximum drawdown as a percentage (0-100).
            results: Extra aggregates (by_category, by_exit_reason, etc.).
        """
        with self._db.cursor() as cur:
            cur.execute(
                COMPLETE_RUN,
                {
                    "id": run_id,
                    "completed_at": datetime.now(timezone.utc),
                    "results": json.dumps(_sanitize(results), default=_json_default),
                    "total_trades": total_trades,
                    "win_rate": win_rate,
                    "total_pnl": total_pnl,
                    "sharpe": sharpe,
                    "max_drawdown": max_drawdown,
                },
            )

    def insert_positions(self, run_id: UUID, trades: list[dict]) -> None:
        """Insert per-trade rows for a completed run.

        Args:
            run_id: The run these trades belong to.
            trades: Trade dicts emitted by BacktestEngine.process_day.
        """
        if not trades:
            return
        with self._db.cursor() as cur:
            cur.executemany(
                INSERT_POSITION,
                [_trade_params(run_id, t) for t in trades],
            )

    def get_latest(self) -> dict | None:
        """Return the most recently completed run, or None if none exist."""
        with self._db.cursor() as cur:
            cur.execute(SELECT_LATEST)
            return cur.fetchone()

    def get_by_id(self, run_id: UUID) -> dict | None:
        """Return a single run by ID, or None if it does not exist."""
        with self._db.cursor() as cur:
            cur.execute(SELECT_BY_ID, {"id": run_id})
            return cur.fetchone()

    def get_positions(self, run_id: UUID) -> list[dict]:
        """Return all per-trade rows for a run, ordered by entry date."""
        with self._db.cursor() as cur:
            cur.execute(SELECT_POSITIONS_BY_RUN, {"run_id": run_id})
            return cur.fetchall()

    def list_runs(self, limit: int = 20) -> list[dict]:
        """Return a summary list of recent runs, newest first."""
        with self._db.cursor() as cur:
            cur.execute(LIST_RUNS, {"limit": limit})
            return cur.fetchall()


def _trade_params(run_id: UUID, trade: dict) -> dict:
    """Map an engine trade dict to INSERT_POSITION named params."""
    return {
        "run_id": run_id,
        "polymarket_id": trade["polymarket_id"],
        "question": trade["question"],
        "category": trade.get("category", "unknown"),
        "side": trade.get("side", "BUY"),
        "entry_price": trade["entry_price"],
        "exit_price": trade.get("exit_price"),
        "target_price": trade.get("target_price", trade["entry_price"]),
        "kelly_fraction": trade["kelly_fraction"],
        "position_size": trade["position_size"],
        "pnl": trade["pnl"],
        "exit_reason": trade.get("exit_reason"),
        "entry_date": trade["entry_date"],
        "exit_date": trade.get("exit_date"),
        "estimator_prob": trade["estimator_prob"],
        "market_price": trade["market_price"],
    }


def _sanitize(obj: Any) -> Any:
    """Recursively replace non-finite floats with None for JSON safety."""
    if isinstance(obj, float):
        return None if (obj != obj or obj == float("inf") or obj == float("-inf")) else obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


def _json_default(obj: Any) -> Any:
    """Coerce types that json.dumps can't handle natively."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

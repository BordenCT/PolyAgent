"""Backtest engine — replays historical data through the full pipeline."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from polyagent.backtest.data_loader import DataLoader, MarketSnapshot
from polyagent.backtest.estimator import BaseEstimator
from polyagent.models import Vote, VoteAction
from polyagent.services.executor import ExecutorService
from polyagent.services.exit_monitor import ExitMonitorService
from polyagent.services.scanner import ScannerService

logger = logging.getLogger("polyagent.backtest.engine")


@dataclass
class BacktestResult:
    """Aggregated backtest results."""

    trades: list[dict]
    start_date: date
    end_date: date
    estimator_name: str
    bankroll: float

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def winners(self) -> int:
        return sum(1 for t in self.trades if t["pnl"] > 0)

    @property
    def losers(self) -> int:
        return sum(1 for t in self.trades if t["pnl"] <= 0)

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return (self.winners / self.total_trades) * 100

    @property
    def total_pnl(self) -> float:
        return sum(t["pnl"] for t in self.trades)

    @property
    def avg_pnl(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.total_pnl / self.total_trades

    @property
    def sharpe(self) -> float:
        if self.total_trades < 2:
            return 0.0
        pnls = [t["pnl"] for t in self.trades]
        mean = sum(pnls) / len(pnls)
        variance = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
        std = variance ** 0.5
        if std == 0:
            return 0.0
        return mean / std

    @property
    def max_drawdown(self) -> float:
        """Maximum drawdown as a percentage."""
        if not self.trades:
            return 0.0
        equity = self.bankroll
        peak = equity
        max_dd = 0.0
        for t in self.trades:
            equity += t["pnl"]
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)
        return max_dd * 100

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t["pnl"] for t in self.trades if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in self.trades if t["pnl"] < 0))
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    @property
    def by_category(self) -> dict[str, dict]:
        """Breakdown by market category."""
        cats: dict[str, list] = {}
        for t in self.trades:
            cat = t.get("category", "unknown")
            cats.setdefault(cat, []).append(t)

        return {
            cat: {
                "trades": len(trades),
                "pnl": sum(t["pnl"] for t in trades),
                "win_rate": sum(1 for t in trades if t["pnl"] > 0) / max(len(trades), 1) * 100,
            }
            for cat, trades in cats.items()
        }

    @property
    def by_exit_reason(self) -> dict[str, int]:
        """Count by exit reason."""
        reasons: dict[str, int] = {}
        for t in self.trades:
            r = t.get("exit_reason", "UNKNOWN")
            reasons[r] = reasons.get(r, 0) + 1
        return reasons


class BacktestEngine:
    """Replays historical market data through the trading pipeline."""

    def __init__(
        self,
        scanner: ScannerService,
        executor: ExecutorService,
        exit_monitor: ExitMonitorService,
        estimator: BaseEstimator,
    ) -> None:
        self._scanner = scanner
        self._executor = executor
        self._exit_monitor = exit_monitor
        self._estimator = estimator

    def run(
        self,
        snapshots: list[MarketSnapshot],
        resolutions: dict[str, dict],
        start_date: date,
        end_date: date,
        bankroll: float = 800.0,
    ) -> BacktestResult:
        """Run a full backtest over the given data."""
        by_day = DataLoader.group_by_day(snapshots)
        all_trades = []

        sorted_days = sorted(d for d in by_day if start_date <= d <= end_date)
        logger.info(
            "Running backtest: %s to %s (%d days, estimator=%s)",
            start_date, end_date, len(sorted_days), self._estimator.name,
        )

        progress = Progress(
            SpinnerColumn(),
            TextColumn("[cyan]Backtesting"),
            BarColumn(bar_width=40),
            TextColumn("[green]{task.completed}/{task.total} days"),
            TextColumn("[yellow]{task.fields[trades]} trades"),
            TimeElapsedColumn(),
        )
        with progress:
            task = progress.add_task("backtest", total=len(sorted_days), trades=0)
            for day in sorted_days:
                day_snapshots = by_day[day]
                day_trades = self.process_day(day_snapshots, resolutions, day)
                all_trades.extend(day_trades)
                progress.update(task, advance=1, trades=len(all_trades))

        result = BacktestResult(
            trades=all_trades,
            start_date=start_date,
            end_date=end_date,
            estimator_name=self._estimator.name,
            bankroll=bankroll,
        )
        logger.info(
            "Backtest complete: %d trades, %.1f%% win rate, $%.2f P&L, Sharpe %.2f",
            result.total_trades, result.win_rate, result.total_pnl, result.sharpe,
        )
        return result

    def process_day(
        self,
        snapshots: list[MarketSnapshot],
        resolutions: dict[str, dict],
        current_date: date,
    ) -> list[dict]:
        """Process one day of market data. Returns list of completed trade dicts."""
        daily_markets = DataLoader.aggregate_daily_markets(snapshots)
        trades = []

        for market_id, snapshot in daily_markets.items():
            resolution = resolutions.get(market_id, {})
            market = snapshot.to_market_data(hours_to_resolution=48.0)

            # Get probability estimate
            estimate = self._estimator.estimate(
                market_id,
                outcome=resolution.get("outcome"),
                final_price=resolution.get("final_price"),
                market_price=float(snapshot.price),
            )

            # Run scanner
            score = self._scanner.score_market(market, estimate)
            if score is None:
                continue

            # Simulate consensus (in backtest, use simplified 2-vote BUY)
            from polyagent.models import Thesis, ThesisChecks
            thesis = Thesis.create(
                market_id=None,
                claude_estimate=estimate,
                confidence=0.80,
                checks=ThesisChecks(base_rate=True, news=True, whale=False, disposition=True),
                thesis_text=f"Backtest thesis for {market_id}",
            )

            votes = [
                Vote(action=VoteAction.BUY, confidence=0.8, reason="backtest"),
                Vote(action=VoteAction.BUY, confidence=0.7, reason="backtest"),
                Vote(action=VoteAction.HOLD, confidence=0.3, reason="backtest"),
            ]

            position = self._executor.execute(
                thesis=thesis,
                votes=votes,
                market_price=snapshot.price,
            )
            if position is None:
                continue

            # Simulate exit using resolution data
            final_price = Decimal(str(resolution.get("final_price", float(snapshot.price))))
            exit_reason = self._exit_monitor.check_exit(
                entry_price=snapshot.price,
                target_price=position.target_price,
                current_price=final_price,
                volume_10min=0,
                avg_volume_10min=1,
                hours_since_entry=48.0,
            )
            if exit_reason is None:
                exit_reason_str = "HELD_TO_RESOLUTION"
            else:
                exit_reason_str = exit_reason.value

            pnl = float(self._exit_monitor.calculate_pnl(
                entry_price=snapshot.price,
                exit_price=final_price,
                position_size=position.position_size,
                side=position.side.value,
            ))

            trades.append({
                "polymarket_id": market_id,
                "question": snapshot.question,
                "category": snapshot.category,
                "entry_price": float(snapshot.price),
                "exit_price": float(final_price),
                "position_size": float(position.position_size),
                "kelly_fraction": position.kelly_fraction,
                "pnl": pnl,
                "exit_reason": exit_reason_str,
                "entry_date": str(current_date),
                "estimator_prob": estimate,
                "market_price": float(snapshot.price),
            })

        return trades

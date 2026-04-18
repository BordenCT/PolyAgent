"""Path-aware backtest engine.

Replays historical trade data hour-by-hour and simulates the full entry + exit
pipeline (scanner -> executor -> exit monitor) against each bar. Unlike a
resolution-only backtest, exits fire when a bar's high/low touches the target
(TARGET_HIT), when hourly volume spikes vs the entry baseline (VOLUME_EXIT),
or when the thesis goes stale (STALE_THESIS). Only positions still open at the
market's final observed bar are force-closed at the resolution price.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

import polars as pl

from polyagent.backtest.data_loader import DataLoader, HourlyBar
from polyagent.backtest.estimator import BaseEstimator
from polyagent.models import ExitReason, Thesis, ThesisChecks, Vote, VoteAction
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
        reasons: dict[str, int] = {}
        for t in self.trades:
            r = t.get("exit_reason", "UNKNOWN")
            reasons[r] = reasons.get(r, 0) + 1
        return reasons


@dataclass
class _OpenPosition:
    """Ephemeral bookkeeping for a live backtest position."""

    market_id: str
    question: str
    category: str
    side: str
    entry_price: Decimal
    target_price: Decimal
    position_size: Decimal
    kelly_fraction: float
    estimator_prob: float
    entry_hour: datetime
    entry_hourly_volume: float
    trailing_volume: float


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
        bars: list[HourlyBar],
        resolutions: dict[str, dict],
        start_date: date,
        end_date: date,
        bankroll: float = 800.0,
        market_metadata: dict[str, dict] | None = None,
    ) -> BacktestResult:
        """Replay the bars chronologically, firing entries and exits on each hour."""
        metadata = market_metadata or {}
        filtered = [
            b for b in bars
            if start_date <= b.hour.date() <= end_date
        ]
        filtered.sort(key=lambda b: (b.hour, b.market_id))
        by_hour = DataLoader.group_by_hour(filtered)
        sorted_hours = sorted(by_hour.keys())
        last_bar_hour: dict[str, datetime] = {}
        for b in filtered:
            last_bar_hour[b.market_id] = b.hour

        logger.info(
            "Running backtest: %s to %s (%d hours, %d bars, estimator=%s)",
            start_date, end_date, len(sorted_hours), len(filtered), self._estimator.name,
        )

        open_positions: dict[str, _OpenPosition] = {}
        evaluated: set[str] = set()
        trades: list[dict] = []
        rolling_volumes: dict[str, deque] = {}
        running_pnl = 0.0

        def _equity_label() -> str:
            equity = bankroll + running_pnl
            pct = (running_pnl / bankroll * 100) if bankroll > 0 else 0.0
            sign = "+" if running_pnl >= 0 else ""
            return f"${equity:,.2f} ({sign}{pct:.1f}%)"

        progress = Progress(
            SpinnerColumn(),
            TextColumn("[cyan]Backtesting"),
            BarColumn(bar_width=40),
            TextColumn("[green]{task.completed}/{task.total} hours"),
            TextColumn("[yellow]{task.fields[trades]} trades"),
            TextColumn("[magenta]{task.fields[equity]}"),
            TimeElapsedColumn(),
        )
        with progress:
            task = progress.add_task(
                "backtest", total=len(sorted_hours), trades=0, equity=_equity_label(),
            )

            for hour in sorted_hours:
                hour_bars = by_hour[hour]

                for bar in hour_bars:
                    vol_deque = rolling_volumes.setdefault(bar.market_id, deque(maxlen=24))
                    vol_deque.append(float(bar.volume))

                    if bar.market_id in open_positions:
                        closed = self._maybe_close(
                            open_positions[bar.market_id], bar, rolling=vol_deque,
                        )
                        if closed is not None:
                            trades.append(closed)
                            running_pnl += closed["pnl"]
                            del open_positions[bar.market_id]
                        # If the bar we just processed is the market's last
                        # observed bar and we still hold — force close.
                        elif last_bar_hour.get(bar.market_id) == bar.hour:
                            forced = self._force_close(
                                open_positions[bar.market_id], bar, resolutions,
                            )
                            trades.append(forced)
                            running_pnl += forced["pnl"]
                            del open_positions[bar.market_id]

                    elif bar.market_id not in evaluated:
                        evaluated.add(bar.market_id)
                        position = self._maybe_enter(
                            bar,
                            metadata=metadata,
                            resolutions=resolutions,
                            rolling_volume=sum(vol_deque),
                        )
                        if position is not None:
                            open_positions[bar.market_id] = position

                progress.update(task, advance=1, trades=len(trades), equity=_equity_label())

        # Markets whose last bar was outside the requested range still need closing.
        for market_id, pos in list(open_positions.items()):
            resolution = resolutions.get(market_id, {})
            final_price = resolution.get("final_price")
            if final_price is None:
                continue
            tail = self._close(
                pos, exit_price=Decimal(str(round(float(final_price), 6))),
                reason="RESOLUTION", exit_hour=pos.entry_hour,
            )
            trades.append(tail)
            running_pnl += tail["pnl"]

        result = BacktestResult(
            trades=trades,
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

    def run_polars(
        self,
        df: pl.DataFrame,
        resolutions: dict[str, dict],
        start_date: date,
        end_date: date,
        bankroll: float = 800.0,
        market_metadata: dict[str, dict] | None = None,
    ) -> BacktestResult:
        """Stream through a candles DataFrame market-by-market.

        Unlike ``run()`` which holds all HourlyBar objects in memory, this
        method processes one market at a time from the Polars DataFrame,
        keeping peak memory proportional to the largest single market (~1000
        bars) rather than the full dataset (48M+ rows).
        """
        metadata = market_metadata or {}

        # Get unique market IDs ordered by their first bar so the progress bar
        # reflects wall-clock time rather than arbitrary insertion order.
        market_ids: list[str] = (
            df.sort("_ts_dt")
              .group_by("market_id")
              .agg(pl.col("_ts_dt").min().alias("first_bar"))
              .sort("first_bar")["market_id"]
              .to_list()
        )

        logger.info(
            "run_polars: %d markets, estimator=%s", len(market_ids), self._estimator.name,
        )

        trades: list[dict] = []
        running_pnl = 0.0

        def _equity_label() -> str:
            equity = bankroll + running_pnl
            pct = (running_pnl / bankroll * 100) if bankroll > 0 else 0.0
            sign = "+" if running_pnl >= 0 else ""
            return f"${equity:,.2f} ({sign}{pct:.1f}%)"

        progress = Progress(
            SpinnerColumn(),
            TextColumn("[cyan]Backtesting"),
            BarColumn(bar_width=40),
            TextColumn("[green]{task.completed}/{task.total} markets"),
            TextColumn("[yellow]{task.fields[trades]} trades"),
            TextColumn("[magenta]{task.fields[equity]}"),
            TimeElapsedColumn(),
        )
        with progress:
            task = progress.add_task(
                "backtest", total=len(market_ids), trades=0, equity=_equity_label(),
            )

            for market_id in market_ids:
                market_df = (
                    df.filter(pl.col("market_id") == market_id)
                      .sort("_ts_dt")
                )

                resolution = resolutions.get(market_id)
                if not resolution or resolution.get("final_price") is None:
                    progress.update(task, advance=1)
                    continue

                meta = metadata.get(market_id, {})
                vol_deque: deque = deque(maxlen=24)
                position: _OpenPosition | None = None
                entered = False

                rows = market_df.iter_rows(named=True)
                bar_list: list[HourlyBar] = []
                for row in rows:
                    ts = row["_ts_dt"]
                    if ts is None:
                        continue
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    hour = ts.replace(minute=0, second=0, microsecond=0)
                    bar = HourlyBar(
                        market_id=market_id,
                        hour=hour,
                        open=Decimal(str(round(float(row.get("open") or 0), 6))),
                        close=Decimal(str(round(float(row.get("close") or 0), 6))),
                        high=Decimal(str(round(float(row.get("high") or 0), 6))),
                        low=Decimal(str(round(float(row.get("low") or 0), 6))),
                        volume=Decimal(str(round(float(row.get("volume") or 0), 2))),
                        first_ts=hour,
                        last_ts=hour,
                        question=meta.get("question", ""),
                        category=meta.get("category", "unknown"),
                        token_id=meta.get("token_id", row.get("token_id") or ""),
                    )
                    bar_list.append(bar)

                for idx, bar in enumerate(bar_list):
                    vol_deque.append(float(bar.volume))

                    if not entered:
                        position = self._maybe_enter(
                            bar,
                            metadata=metadata,
                            resolutions=resolutions,
                            # Candle volume = tick count, not USD — use a fixed
                            # depth that passes the scanner's min_depth filter.
                            rolling_volume=max(sum(vol_deque), 1000.0),
                        )
                        entered = True
                    elif position is not None:
                        closed = self._maybe_close(position, bar, rolling=vol_deque)
                        if closed is not None:
                            trades.append(closed)
                            running_pnl += closed["pnl"]
                            position = None
                        elif idx == len(bar_list) - 1:
                            forced = self._force_close(position, bar, resolutions)
                            trades.append(forced)
                            running_pnl += forced["pnl"]
                            position = None

                progress.update(
                    task, advance=1, trades=len(trades), equity=_equity_label(),
                )

        result = BacktestResult(
            trades=trades,
            start_date=start_date,
            end_date=end_date,
            estimator_name=self._estimator.name,
            bankroll=bankroll,
        )
        logger.info(
            "run_polars complete: %d trades, %.1f%% win rate, $%.2f P&L, Sharpe %.2f",
            result.total_trades, result.win_rate, result.total_pnl, result.sharpe,
        )
        return result

    def _maybe_enter(
        self,
        bar: HourlyBar,
        metadata: dict[str, dict],
        resolutions: dict[str, dict],
        rolling_volume: float,
    ) -> _OpenPosition | None:
        """Decide whether to enter on this bar. Returns an open position or None."""
        resolution = resolutions.get(bar.market_id)
        if not resolution or resolution.get("final_price") is None:
            return None

        meta = metadata.get(bar.market_id, {})
        bar.question = meta.get("question", bar.question)
        bar.category = meta.get("category", bar.category)
        bar.token_id = meta.get("token_id", bar.token_id)

        market = bar.to_market_data(
            hours_to_resolution=48.0,
            volume_24h=Decimal(str(round(rolling_volume, 2))),
        )

        estimate = self._estimator.estimate(
            bar.market_id,
            outcome=resolution.get("outcome"),
            final_price=float(resolution["final_price"]),
            market_price=float(bar.close),
            question=bar.question,
        )

        score = self._scanner.score_market(market, estimate)
        if score is None:
            return None

        thesis = Thesis.create(
            market_id=None,
            claude_estimate=estimate,
            confidence=0.80,
            checks=ThesisChecks(base_rate=True, news=True, whale=False, disposition=True),
            thesis_text=f"Backtest thesis for {bar.market_id}",
        )

        votes = [
            Vote(action=VoteAction.BUY, confidence=0.8, reason="backtest"),
            Vote(action=VoteAction.BUY, confidence=0.7, reason="backtest"),
            Vote(action=VoteAction.HOLD, confidence=0.3, reason="backtest"),
        ]

        plan = self._executor.plan(thesis=thesis, votes=votes, market_price=bar.close)
        if plan is None:
            return None

        return _OpenPosition(
            market_id=bar.market_id,
            question=bar.question,
            category=bar.category,
            side=plan.side.value,
            entry_price=plan.market_price,
            target_price=plan.target_price,
            position_size=plan.position_size,
            kelly_fraction=plan.kelly_fraction,
            estimator_prob=estimate,
            entry_hour=bar.hour,
            entry_hourly_volume=float(bar.volume),
            trailing_volume=rolling_volume,
        )

    def _maybe_close(
        self,
        position: _OpenPosition,
        bar: HourlyBar,
        rolling: deque,
    ) -> dict | None:
        """Check the three exit triggers against this bar. Close if any fire."""
        entry_price = float(position.entry_price)
        target_price = float(position.target_price)

        # TARGET_HIT — take-profit fills during the hour if the high touched target.
        if position.side == "BUY" and float(bar.high) >= target_price and target_price > entry_price:
            return self._close(
                position,
                exit_price=Decimal(str(round(target_price, 6))),
                reason=ExitReason.TARGET_HIT.value,
                exit_hour=bar.hour,
            )

        hours_since_entry = (bar.hour - position.entry_hour).total_seconds() / 3600.0

        # VOLUME_EXIT — this bar's hourly volume vs the entry bar's hourly volume.
        entry_hourly = position.entry_hourly_volume
        current_hourly = float(bar.volume)
        if (
            entry_hourly > 0
            and hours_since_entry >= 1
            and current_hourly > entry_hourly * self._exit_monitor.volume_multiplier
        ):
            return self._close(
                position,
                exit_price=bar.close,
                reason=ExitReason.VOLUME_EXIT.value,
                exit_hour=bar.hour,
            )

        # STALE_THESIS — configured stale window elapsed with sub-2% movement.
        if hours_since_entry > self._exit_monitor.stale_hours:
            move = abs(float(bar.close) - entry_price) / entry_price if entry_price > 0 else 0.0
            if move < self._exit_monitor.stale_threshold:
                return self._close(
                    position,
                    exit_price=bar.close,
                    reason=ExitReason.STALE_THESIS.value,
                    exit_hour=bar.hour,
                )

        return None

    def _force_close(
        self,
        position: _OpenPosition,
        bar: HourlyBar,
        resolutions: dict[str, dict],
    ) -> dict:
        """Close at the market's resolution price at end of observed life."""
        resolution = resolutions.get(position.market_id, {})
        final_price = resolution.get("final_price")
        exit_price = (
            Decimal(str(round(float(final_price), 6))) if final_price is not None
            else bar.close
        )
        return self._close(
            position,
            exit_price=exit_price,
            reason="RESOLUTION",
            exit_hour=bar.hour,
        )

    def _close(
        self,
        position: _OpenPosition,
        exit_price: Decimal,
        reason: str,
        exit_hour: datetime,
    ) -> dict:
        pnl = float(self._exit_monitor.calculate_pnl(
            entry_price=position.entry_price,
            exit_price=exit_price,
            position_size=position.position_size,
            side=position.side,
        ))
        return {
            "polymarket_id": position.market_id,
            "question": position.question,
            "category": position.category,
            "side": position.side,
            "entry_price": float(position.entry_price),
            "exit_price": float(exit_price),
            "target_price": float(position.target_price),
            "position_size": float(position.position_size),
            "kelly_fraction": position.kelly_fraction,
            "pnl": pnl,
            "exit_reason": reason,
            "entry_date": position.entry_hour.isoformat(),
            "exit_date": exit_hour.isoformat(),
            "estimator_prob": position.estimator_prob,
            "market_price": float(position.entry_price),
        }


def _hours_until(resolution_date: str | None, from_hour: datetime) -> float:
    """Best-effort hours between now and the market's resolution timestamp."""
    if not resolution_date:
        return 48.0
    try:
        res = datetime.fromisoformat(str(resolution_date).replace("Z", "+00:00"))
    except ValueError:
        return 48.0
    if res.tzinfo is None:
        res = res.replace(tzinfo=timezone.utc)
    delta_hours = (res - from_hour).total_seconds() / 3600.0
    return max(0.0, delta_hours)

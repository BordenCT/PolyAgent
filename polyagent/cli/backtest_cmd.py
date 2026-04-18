"""Backtest CLI command."""
from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

import click
from rich.console import Console
from rich.table import Table

from polyagent.backtest.data_loader import DataLoader
from polyagent.backtest.engine import BacktestEngine, BacktestResult
from polyagent.backtest.estimator import CachedClaudeEstimator, HistoricalEstimator, MidpointEstimator, OllamaEstimator
from polyagent.backtest.report import print_report
from polyagent.data.repositories.backtests import BacktestRepository
from polyagent.infra.config import Settings
from polyagent.infra.database import Database
from polyagent.services.executor import ExecutorService
from polyagent.services.exit_monitor import ExitMonitorService
from polyagent.services.scanner import ScannerService


ESTIMATORS = {
    "historical": HistoricalEstimator,
    "midpoint": MidpointEstimator,
    "cached-claude": CachedClaudeEstimator,
    "ollama": OllamaEstimator,
}


@click.command()
@click.option(
    "--start", type=click.DateTime(formats=["%Y-%m-%d"]),
    default="2025-01-01", help="Backtest start date (YYYY-MM-DD)",
)
@click.option(
    "--end", type=click.DateTime(formats=["%Y-%m-%d"]),
    default="2026-04-01", help="Backtest end date (YYYY-MM-DD)",
)
@click.option(
    "--estimator", type=click.Choice(list(ESTIMATORS.keys())),
    default="historical", help="Probability estimator strategy",
)
@click.option("--bankroll", type=float, default=None, help="Starting bankroll (default: from config)")
@click.option("--kelly-max", type=float, default=None, help="Max Kelly fraction (default: from config)")
@click.option("--data-dir", type=click.Path(exists=True), default=None, help="Path to poly_data directory")
@click.option("--report", "show_report", is_flag=True, help="Show the most recent persisted backtest run")
@click.option("--list", "list_runs_flag", is_flag=True, help="List recent persisted backtest runs")
@click.option("--run-id", type=str, default=None, help="Show a specific persisted run by ID")
@click.option("--compare", is_flag=True, help="Compare results across estimators")
def backtest(start, end, estimator, bankroll, kelly_max, data_dir, show_report, list_runs_flag, run_id, compare):
    """Run a backtest against historical poly_data, or view past persisted runs.

    Each backtest is persisted to the database so you can re-view results later
    via --report (latest), --list (browse), or --run-id <uuid> (specific run).

    Examples:

        polyagent backtest --start 2025-01-01 --end 2026-01-01

        polyagent backtest --estimator midpoint  # sanity check

        polyagent backtest --report              # show the most recent run

        polyagent backtest --list                # list past runs

        polyagent backtest --run-id <uuid>       # show a specific run
    """
    console = Console()
    settings = Settings.from_env()

    if list_runs_flag:
        _list_runs(console, settings)
        return

    if show_report or run_id:
        _show_persisted_run(console, settings, run_id)
        return

    effective_bankroll = bankroll or settings.bankroll
    effective_kelly = kelly_max or settings.kelly_max_fraction
    effective_data_dir = data_dir or "~/poly_data"

    start_date = start.date() if hasattr(start, "date") else start
    end_date = end.date() if hasattr(end, "date") else end

    if compare:
        _run_comparison(console, settings, effective_data_dir, start_date, end_date, effective_bankroll, effective_kelly)
        return

    scanner = ScannerService(
        min_gap=settings.min_gap,
        min_depth=settings.min_depth,
        min_hours=settings.min_hours,
        max_hours=settings.max_hours,
    )
    executor = ExecutorService(
        kelly_max_fraction=effective_kelly,
        bankroll=effective_bankroll,
        paper_trade=True,
    )
    exit_monitor = ExitMonitorService(
        target_pct=settings.exit_target_pct,
        volume_multiplier=settings.exit_volume_multiplier,
        stale_hours=settings.exit_stale_hours,
        stale_threshold=settings.exit_stale_threshold,
    )

    estimator_cls = ESTIMATORS[estimator]
    est = estimator_cls()

    console.print(f"[cyan]Loading historical data from {effective_data_dir}...[/cyan]")
    loader = DataLoader(effective_data_dir)

    resolutions = loader.load_resolutions()
    market_metadata = loader.load_market_metadata()

    engine = BacktestEngine(
        scanner=scanner,
        executor=executor,
        exit_monitor=exit_monitor,
        estimator=est,
    )

    parameters = {
        "bankroll": effective_bankroll,
        "kelly_max_fraction": effective_kelly,
        "min_gap": settings.min_gap,
        "min_depth": settings.min_depth,
        "min_hours": settings.min_hours,
        "max_hours": settings.max_hours,
    }

    from pathlib import Path
    candles_path = Path(effective_data_dir) / "processed" / "candles.csv"

    if candles_path.exists():
        try:
            candles_df = loader.load_candles_df(start_date=start_date, end_date=end_date)
        except FileNotFoundError as e:
            console.print(f"[red]Error: {e}[/red]")
            return

        resolution_ids = set(resolutions.keys())
        matched = resolution_ids & set(market_metadata.keys())
        n_markets = candles_df["market_id"].n_unique()
        console.print(
            f"[cyan]Running backtest: {start_date} to {end_date} "
            f"({len(candles_df):,} candle rows, {n_markets:,} resolved markets, "
            f"{len(resolutions):,} resolutions, {len(matched)}/{len(resolution_ids)} with metadata, "
            f"estimator={estimator})[/cyan]"
        )
        result = engine.run_polars(
            df=candles_df,
            resolutions=resolutions,
            start_date=start_date,
            end_date=end_date,
            bankroll=effective_bankroll,
            market_metadata=market_metadata,
        )
    else:
        try:
            bars = loader.load_hourly_bars(start_date=start_date, end_date=end_date)
        except FileNotFoundError as e:
            console.print(f"[red]Error: {e}[/red]")
            console.print("[dim]Run 'polyagent ingest --snapshot' first[/dim]")
            return

        resolution_ids = set(resolutions.keys())
        matched = resolution_ids & set(market_metadata.keys())
        console.print(
            f"[cyan]Running backtest: {start_date} to {end_date} "
            f"({len(bars):,} hourly bars, {len(resolutions):,} resolutions, "
            f"{len(matched)}/{len(resolution_ids)} with metadata, estimator={estimator})[/cyan]"
        )
        result = engine.run(
            bars=bars,
            resolutions=resolutions,
            start_date=start_date,
            end_date=end_date,
            bankroll=effective_bankroll,
            market_metadata=market_metadata,
        )

    _flush_estimator(est)

    run_uuid = _persist_run(console, settings, result, estimator, parameters)
    print_report(result, console)
    if run_uuid:
        console.print(f"[dim]Saved run {run_uuid} to database.[/dim]")


def _run_comparison(console, settings, data_dir, start_date, end_date, bankroll, kelly_max):
    """Run all estimators and compare results side-by-side."""
    loader = DataLoader(data_dir)
    try:
        bars = loader.load_bars(start_date=start_date, end_date=end_date)
    except FileNotFoundError as e:
        console.print(f"[red]Error: {e}[/red]")
        return

    resolutions = loader.load_resolutions()
    market_metadata = loader.load_market_metadata()

    scanner = ScannerService(
        min_gap=settings.min_gap, min_depth=settings.min_depth,
        min_hours=settings.min_hours, max_hours=settings.max_hours,
    )
    executor = ExecutorService(kelly_max_fraction=kelly_max, bankroll=bankroll, paper_trade=True)
    exit_monitor = ExitMonitorService(
        target_pct=settings.exit_target_pct, volume_multiplier=settings.exit_volume_multiplier,
        stale_hours=settings.exit_stale_hours, stale_threshold=settings.exit_stale_threshold,
    )

    table = Table(title="Estimator Comparison")
    table.add_column("Estimator", style="cyan")
    table.add_column("Trades", justify="right")
    table.add_column("Win Rate", justify="right")
    table.add_column("P&L", justify="right")
    table.add_column("Sharpe", justify="right")
    table.add_column("Max DD", justify="right")
    table.add_column("Run ID", style="dim")

    parameters = {
        "bankroll": bankroll,
        "kelly_max_fraction": kelly_max,
        "min_gap": settings.min_gap,
        "min_depth": settings.min_depth,
        "min_hours": settings.min_hours,
        "max_hours": settings.max_hours,
    }

    for name, cls in ESTIMATORS.items():
        console.print(f"[dim]Running {name}...[/dim]")
        est = cls()
        engine = BacktestEngine(scanner=scanner, executor=executor, exit_monitor=exit_monitor, estimator=est)
        result = engine.run(bars, resolutions, start_date, end_date, bankroll, market_metadata)
        _flush_estimator(est)
        run_uuid = _persist_run(console, settings, result, name, parameters)
        pnl_style = "green" if result.total_pnl >= 0 else "red"
        table.add_row(
            name, str(result.total_trades), f"{result.win_rate:.1f}%",
            f"[{pnl_style}]${result.total_pnl:+,.2f}[/{pnl_style}]",
            f"{result.sharpe:.2f}", f"{result.max_drawdown:.1f}%",
            str(run_uuid)[:8] if run_uuid else "-",
        )

    console.print(table)


def _flush_estimator(est) -> None:
    """Flush an estimator's on-disk cache if it supports one."""
    flush = getattr(est, "flush", None)
    if callable(flush):
        flush()


def _persist_run(
    console: Console,
    settings: Settings,
    result: BacktestResult,
    estimator_name: str,
    parameters: dict[str, Any],
) -> UUID | None:
    """Persist a completed backtest to the database.

    Returns the run UUID, or None if persistence failed (errors are logged
    and do not stop the caller from printing the on-screen report).
    """
    db = None
    try:
        db = Database(settings)
        repo = BacktestRepository(db)
        run_id = repo.create_run(
            date_start=result.start_date,
            date_end=result.end_date,
            estimator=estimator_name,
            parameters=parameters,
        )
        repo.insert_positions(run_id, result.trades)
        repo.complete_run(
            run_id=run_id,
            total_trades=result.total_trades,
            win_rate=result.win_rate,
            total_pnl=result.total_pnl,
            sharpe=result.sharpe,
            max_drawdown=result.max_drawdown,
            results={
                "avg_pnl": result.avg_pnl,
                "winners": result.winners,
                "losers": result.losers,
                "profit_factor": result.profit_factor,
                "by_category": result.by_category,
                "by_exit_reason": result.by_exit_reason,
            },
        )
        return run_id
    except Exception as e:
        console.print(f"[yellow]Warning: failed to persist backtest run: {e}[/yellow]")
        return None
    finally:
        if db is not None:
            db.close()


def _show_persisted_run(console: Console, settings: Settings, run_id_str: str | None) -> None:
    """Show a persisted backtest using the existing print_report renderer."""
    db = None
    try:
        db = Database(settings)
        repo = BacktestRepository(db)

        if run_id_str:
            try:
                target_id = UUID(run_id_str)
            except ValueError:
                console.print(f"[red]Invalid run ID: {run_id_str}[/red]")
                return
            run = repo.get_by_id(target_id)
        else:
            run = repo.get_latest()

        if run is None:
            console.print("[yellow]No persisted backtest runs found. Run a backtest first.[/yellow]")
            return

        trades = repo.get_positions(run["id"])
        result = _result_from_rows(run, trades)

        console.print(
            f"[dim]Run {run['id']} — started {run['started_at']} — estimator {run['estimator']}[/dim]"
        )
        print_report(result, console)
    except Exception as e:
        console.print(f"[red]Error loading persisted run: {e}[/red]")
    finally:
        if db is not None:
            db.close()


def _list_runs(console: Console, settings: Settings) -> None:
    """Print a table of recent persisted runs."""
    db = None
    try:
        db = Database(settings)
        repo = BacktestRepository(db)
        runs = repo.list_runs(limit=20)
    except Exception as e:
        console.print(f"[red]Error loading run list: {e}[/red]")
        return
    finally:
        if db is not None:
            db.close()

    if not runs:
        console.print("[yellow]No persisted backtest runs found.[/yellow]")
        return

    table = Table(title="Recent Backtest Runs")
    table.add_column("Run ID", style="cyan")
    table.add_column("Started", style="dim")
    table.add_column("Range")
    table.add_column("Estimator")
    table.add_column("Trades", justify="right")
    table.add_column("Win Rate", justify="right")
    table.add_column("P&L", justify="right")
    table.add_column("Sharpe", justify="right")
    table.add_column("Status", justify="right")

    for row in runs:
        pnl = float(row.get("total_pnl") or 0)
        pnl_style = "green" if pnl >= 0 else "red"
        status = "done" if row.get("completed_at") else "incomplete"
        table.add_row(
            str(row["id"])[:8],
            row["started_at"].strftime("%Y-%m-%d %H:%M") if row.get("started_at") else "-",
            f"{row['date_start']} -> {row['date_end']}",
            row["estimator"],
            str(row.get("total_trades") or 0),
            f"{float(row.get('win_rate') or 0):.1f}%",
            f"[{pnl_style}]${pnl:+,.2f}[/{pnl_style}]",
            f"{float(row.get('sharpe') or 0):.2f}",
            status,
        )

    console.print(table)


def _result_from_rows(run: dict, trades: list[dict]) -> BacktestResult:
    """Rebuild a BacktestResult from DB rows so print_report can render it."""
    rebuilt_trades = [
        {
            "polymarket_id": t["polymarket_id"],
            "question": t["question"],
            "category": t.get("category", "unknown"),
            "pnl": float(t["pnl"] or 0),
            "exit_reason": t.get("exit_reason", "UNKNOWN"),
            "entry_price": float(t["entry_price"]),
            "exit_price": float(t["exit_price"]) if t.get("exit_price") is not None else None,
        }
        for t in trades
    ]

    return BacktestResult(
        trades=rebuilt_trades,
        start_date=run["date_start"],
        end_date=run["date_end"],
        estimator_name=run["estimator"],
        bankroll=_extract_bankroll(run),
    )


def _extract_bankroll(run: dict) -> float:
    """Pull bankroll out of the stored parameters JSON, defaulting to 800."""
    params = run.get("parameters") or {}
    if isinstance(params, str):
        import json
        try:
            params = json.loads(params)
        except (ValueError, TypeError):
            params = {}
    return float(params.get("bankroll", 800.0))

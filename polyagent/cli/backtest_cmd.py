"""Backtest CLI command."""
from __future__ import annotations

from datetime import date

import click
from rich.console import Console

from polyagent.backtest.data_loader import DataLoader
from polyagent.backtest.engine import BacktestEngine
from polyagent.backtest.estimator import CachedClaudeEstimator, HistoricalEstimator, MidpointEstimator, OllamaEstimator
from polyagent.backtest.report import print_report
from polyagent.services.executor import ExecutorService
from polyagent.services.exit_monitor import ExitMonitorService
from polyagent.services.scanner import ScannerService
from polyagent.infra.config import Settings


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
@click.option("--report", "show_report", is_flag=True, help="Show results of the last backtest run")
@click.option("--compare", is_flag=True, help="Compare results across estimators")
def backtest(start, end, estimator, bankroll, kelly_max, data_dir, show_report, compare):
    """Run a backtest against historical poly_data.

    Replays the full scanner -> executor -> exit pipeline against historical
    market data. Use different estimators to test strategy performance.

    Examples:

        polyagent backtest --start 2025-01-01 --end 2026-01-01

        polyagent backtest --estimator midpoint  # sanity check

        polyagent backtest --bankroll 2000 --kelly-max 0.15
    """
    console = Console()
    settings = Settings.from_env()

    effective_bankroll = bankroll or settings.bankroll
    effective_kelly = kelly_max or settings.kelly_max_fraction
    effective_data_dir = data_dir or "~/poly_data"

    start_date = start.date() if hasattr(start, "date") else start
    end_date = end.date() if hasattr(end, "date") else end

    if compare:
        _run_comparison(console, settings, effective_data_dir, start_date, end_date, effective_bankroll, effective_kelly)
        return

    # Build services
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

    # Load data
    console.print(f"[cyan]Loading historical data from {effective_data_dir}...[/cyan]")
    loader = DataLoader(effective_data_dir)
    try:
        snapshots = loader.load_trades(start_date=start_date, end_date=end_date)
    except FileNotFoundError as e:
        console.print(f"[red]Error: {e}[/red]")
        console.print("[dim]Run 'polyagent ingest --snapshot' first to download historical data[/dim]")
        return

    resolutions = loader.load_resolutions()
    market_metadata = loader.load_market_metadata()

    console.print(
        f"[cyan]Running backtest: {start_date} to {end_date} "
        f"({len(snapshots)} snapshots, {len(resolutions):,} resolutions, estimator={estimator})[/cyan]"
    )

    engine = BacktestEngine(
        scanner=scanner,
        executor=executor,
        exit_monitor=exit_monitor,
        estimator=est,
    )

    result = engine.run(
        snapshots=snapshots,
        resolutions=resolutions,
        start_date=start_date,
        end_date=end_date,
        bankroll=effective_bankroll,
        market_metadata=market_metadata,
    )

    print_report(result, console)


def _run_comparison(console, settings, data_dir, start_date, end_date, bankroll, kelly_max):
    """Run all estimators and compare results side-by-side."""
    from rich.table import Table

    loader = DataLoader(data_dir)
    try:
        snapshots = loader.load_trades(start_date=start_date, end_date=end_date)
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

    for name, cls in ESTIMATORS.items():
        console.print(f"[dim]Running {name}...[/dim]")
        engine = BacktestEngine(scanner=scanner, executor=executor, exit_monitor=exit_monitor, estimator=cls())
        result = engine.run(snapshots, resolutions, start_date, end_date, bankroll, market_metadata)
        pnl_style = "green" if result.total_pnl >= 0 else "red"
        table.add_row(
            name, str(result.total_trades), f"{result.win_rate:.1f}%",
            f"[{pnl_style}]${result.total_pnl:+,.2f}[/{pnl_style}]",
            f"{result.sharpe:.2f}", f"{result.max_drawdown:.1f}%",
        )

    console.print(table)

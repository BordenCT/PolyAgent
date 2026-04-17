"""Backtest report generation with Rich output."""
from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from polyagent.backtest.engine import BacktestResult


def print_report(result: BacktestResult, console: Console | None = None) -> None:
    """Print a formatted backtest report to the terminal."""
    if console is None:
        console = Console()

    final_equity = result.bankroll + result.total_pnl
    pnl_style = "green" if result.total_pnl >= 0 else "red"

    summary = (
        f"Period: {result.start_date} to {result.end_date}\n"
        f"Estimator: {result.estimator_name}\n"
        f"Bankroll: ${result.bankroll:,.2f} -> [{pnl_style}]${final_equity:,.2f}[/{pnl_style}]\n"
        f"\n"
        f"Total Trades:    {result.total_trades}\n"
        f"Win Rate:        {result.win_rate:.1f}%\n"
        f"Total P&L:       [{pnl_style}]${result.total_pnl:+,.2f}[/{pnl_style}]\n"
        f"Avg P&L/Trade:   ${result.avg_pnl:+,.2f}\n"
        f"Sharpe Ratio:    {result.sharpe:.2f}\n"
        f"Max Drawdown:    {result.max_drawdown:.1f}%\n"
        f"Profit Factor:   {result.profit_factor:.2f}\n"
    )

    console.print(Panel(summary, title="Backtest Report", expand=False))

    # Exit reasons
    reasons = result.by_exit_reason
    if reasons:
        reason_table = Table(title="Exit Reasons")
        reason_table.add_column("Reason", style="cyan")
        reason_table.add_column("Count", justify="right")
        reason_table.add_column("Pct", justify="right")
        total = sum(reasons.values())
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            reason_table.add_row(reason, str(count), f"{count / total * 100:.1f}%")
        console.print(reason_table)

    # By category
    cats = result.by_category
    if cats:
        cat_table = Table(title="By Category")
        cat_table.add_column("Category", style="cyan")
        cat_table.add_column("Trades", justify="right")
        cat_table.add_column("P&L", justify="right")
        cat_table.add_column("Win Rate", justify="right")
        for cat, stats in sorted(cats.items(), key=lambda x: -x[1]["pnl"]):
            cat_pnl = stats["pnl"]
            s = "green" if cat_pnl >= 0 else "red"
            cat_table.add_row(
                cat, str(stats["trades"]),
                f"[{s}]${cat_pnl:+,.2f}[/{s}]",
                f"{stats['win_rate']:.1f}%",
            )
        console.print(cat_table)

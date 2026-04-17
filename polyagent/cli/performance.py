"""Performance command — P&L analytics."""
from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

from polyagent.infra.config import Settings
from polyagent.infra.database import Database


@click.command()
@click.option("--daily", is_flag=True, help="Day-by-day breakdown")
@click.option("--by-strategy", is_flag=True, help="Per-strategy performance")
@click.option("--by-category", is_flag=True, help="Per-category performance")
def perf(daily: bool, by_strategy: bool, by_category: bool):
    """Show bot performance: P&L, win rate, Sharpe, trade count."""
    console = Console()
    settings = Settings.from_env()
    db = Database(settings)

    with db.cursor() as cur:
        cur.execute("""
            SELECT
                COUNT(*) as total_trades,
                COUNT(*) FILTER (WHERE pnl > 0) as winners,
                COALESCE(SUM(pnl), 0) as total_pnl,
                COALESCE(AVG(pnl), 0) as avg_pnl,
                COALESCE(STDDEV(pnl), 0) as stddev_pnl
            FROM positions WHERE status = 'closed'
        """)
        stats = cur.fetchone()

    total = stats["total_trades"]
    winners = stats["winners"]
    win_rate = (winners / total * 100) if total > 0 else 0
    total_pnl = float(stats["total_pnl"])
    avg_pnl = float(stats["avg_pnl"])
    stddev = float(stats["stddev_pnl"])
    sharpe = (avg_pnl / stddev) if stddev > 0 else 0

    table = Table(title="PolyAgent Performance")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    pnl_style = "green" if total_pnl >= 0 else "red"
    table.add_row("Total Trades", str(total))
    table.add_row("Winners", f"{winners} ({win_rate:.1f}%)")
    table.add_row("Total P&L", f"[{pnl_style}]${total_pnl:+,.2f}[/{pnl_style}]")
    table.add_row("Avg P&L/Trade", f"${avg_pnl:+,.2f}")
    table.add_row("Sharpe Ratio", f"{sharpe:.2f}")

    console.print(table)

    if daily:
        with db.cursor() as cur:
            cur.execute("""
                SELECT DATE(closed_at) as day,
                       COUNT(*) as trades,
                       SUM(pnl) as day_pnl
                FROM positions WHERE status = 'closed'
                GROUP BY DATE(closed_at)
                ORDER BY day DESC LIMIT 14
            """)
            days = cur.fetchall()

        day_table = Table(title="Daily P&L")
        day_table.add_column("Date", style="cyan")
        day_table.add_column("Trades", justify="right")
        day_table.add_column("P&L", justify="right")
        for d in days:
            dpnl = float(d["day_pnl"])
            s = "green" if dpnl >= 0 else "red"
            day_table.add_row(str(d["day"]), str(d["trades"]), f"[{s}]${dpnl:+,.2f}[/{s}]")
        console.print(day_table)

    db.close()

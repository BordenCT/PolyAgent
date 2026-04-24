"""Per-class performance analytics — `polyagent class-stats`."""
from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

from polyagent.infra.config import Settings
from polyagent.infra.database import Database


CLASS_STATS_QUERY = """
    SELECT
        m.market_class::text AS class,
        COUNT(*) FILTER (WHERE p.status = 'closed')                 AS trades,
        COUNT(*) FILTER (WHERE p.status = 'closed' AND p.pnl > 0)   AS wins,
        COUNT(*) FILTER (WHERE p.status = 'closed' AND p.pnl <= 0)  AS losses,
        COALESCE(SUM(p.pnl)  FILTER (WHERE p.status = 'closed'), 0) AS total_pnl,
        COALESCE(AVG(p.pnl)  FILTER (WHERE p.status = 'closed'), 0) AS avg_pnl,
        COALESCE(
            AVG(EXTRACT(EPOCH FROM (p.closed_at - p.opened_at)) / 3600)
            FILTER (WHERE p.status = 'closed'), 0
        )                                                           AS avg_hold_h
    FROM markets m
    LEFT JOIN positions p ON p.market_id = m.id
    GROUP BY m.market_class
    ORDER BY total_pnl DESC
"""


@click.command("class-stats")
def class_stats():
    """Show per-class performance: trades, win rate, P&L, hold time."""
    console = Console()
    settings = Settings.from_env()
    db = Database(settings)

    with db.cursor() as cur:
        cur.execute(CLASS_STATS_QUERY)
        rows = cur.fetchall()

    table = Table(title="Per-Class Performance")
    table.add_column("Class", style="cyan")
    table.add_column("Trades", justify="right")
    table.add_column("W/L", justify="right")
    table.add_column("Win%", justify="right")
    table.add_column("Avg P&L", justify="right")
    table.add_column("Total P&L", justify="right")
    table.add_column("Avg Hold", justify="right")

    total_trades = total_wins = total_losses = 0
    grand_total_pnl = 0.0

    for r in rows:
        trades = int(r["trades"] or 0)
        wins = int(r["wins"] or 0)
        losses = int(r["losses"] or 0)
        total_pnl = float(r["total_pnl"] or 0)
        avg_pnl = float(r["avg_pnl"] or 0)
        avg_hold = float(r["avg_hold_h"] or 0)

        total_trades += trades
        total_wins += wins
        total_losses += losses
        grand_total_pnl += total_pnl

        if trades == 0:
            table.add_row(r["class"], "0", "-", "-", "-", "$0.00", "-")
            continue

        win_pct = (wins / trades) * 100
        pnl_style = "green" if total_pnl >= 0 else "red"
        avg_style = "green" if avg_pnl >= 0 else "red"
        table.add_row(
            r["class"],
            str(trades),
            f"{wins}/{losses}",
            f"{win_pct:.0f}%",
            f"[{avg_style}]${avg_pnl:+,.2f}[/{avg_style}]",
            f"[{pnl_style}]${total_pnl:+,.2f}[/{pnl_style}]",
            f"{avg_hold:.0f}h",
        )

    if total_trades:
        total_win_pct = (total_wins / total_trades) * 100
        grand_avg = grand_total_pnl / total_trades
        grand_style = "green" if grand_total_pnl >= 0 else "red"
        avg_style = "green" if grand_avg >= 0 else "red"
        table.add_section()
        table.add_row(
            "TOTAL",
            str(total_trades),
            f"{total_wins}/{total_losses}",
            f"{total_win_pct:.0f}%",
            f"[{avg_style}]${grand_avg:+,.2f}[/{avg_style}]",
            f"[{grand_style}]${grand_total_pnl:+,.2f}[/{grand_style}]",
            "-",
        )

    console.print(table)
    db.close()

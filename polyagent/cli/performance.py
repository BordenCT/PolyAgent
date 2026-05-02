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

    # Reads from polyagent_trades_v (migration 008): a UNION of the main
    # positions ledger and the short-horizon paper ledger, with a `ledger`
    # discriminator. Single source of truth for combined P&L; per-ledger
    # split keeps paper and live distinguishable. For short-horizon-specific
    # drill-down (|edge|, vol), use `polyagent quant-stats`.
    with db.cursor() as cur:
        cur.execute("""
            SELECT
                COUNT(*)                                                      AS total_trades,
                COUNT(*) FILTER (WHERE won)                                   AS winners,
                COALESCE(SUM(pnl), 0)                                         AS total_pnl,
                COALESCE(AVG(pnl), 0)                                         AS avg_pnl,
                COALESCE(STDDEV(pnl), 0)                                      AS stddev_pnl,
                COALESCE(SUM(size), 0)                                        AS total_staked,
                COUNT(*) FILTER (WHERE ledger = 'main')                       AS main_trades,
                COUNT(*) FILTER (WHERE ledger = 'short_horizon')              AS short_trades,
                COALESCE(SUM(pnl) FILTER (WHERE ledger = 'main'), 0)          AS main_pnl,
                COALESCE(SUM(pnl) FILTER (WHERE ledger = 'short_horizon'), 0) AS short_pnl,
                COALESCE(SUM(size) FILTER (WHERE ledger = 'main'), 0)         AS main_staked,
                COALESCE(SUM(size) FILTER (WHERE ledger = 'short_horizon'), 0) AS short_staked
            FROM polyagent_trades_v
        """)
        stats = cur.fetchone()

    total = int(stats["total_trades"] or 0)
    winners = int(stats["winners"] or 0)
    win_rate = (winners / total * 100) if total > 0 else 0
    total_pnl = float(stats["total_pnl"])
    avg_pnl = float(stats["avg_pnl"])
    stddev = float(stats["stddev_pnl"])
    sharpe = (avg_pnl / stddev) if stddev > 0 else 0
    total_staked = float(stats["total_staked"])
    roi = (total_pnl / total_staked * 100) if total_staked > 0 else 0
    main_trades = int(stats["main_trades"] or 0)
    short_trades = int(stats["short_trades"] or 0)
    main_pnl = float(stats["main_pnl"])
    short_pnl = float(stats["short_pnl"])
    main_staked = float(stats["main_staked"])
    short_staked = float(stats["short_staked"])

    table = Table(title="PolyAgent Performance")
    table.add_column("Metric", style="cyan")
    table.add_column("Value")

    pnl_style = "green" if total_pnl >= 0 else "red"
    main_style = "green" if main_pnl >= 0 else "red"
    short_style = "green" if short_pnl >= 0 else "red"
    table.add_row("Total Trades", f"{total}  ({main_trades} main + {short_trades} short_horizon)")
    table.add_row("Winners", f"{winners} ({win_rate:.1f}%)")
    table.add_row("Total Staked", f"${total_staked:,.2f}  (${main_staked:,.2f} main + ${short_staked:,.2f} short)")
    table.add_row("Total P&L", f"[{pnl_style}]${total_pnl:+,.2f}[/{pnl_style}]")
    table.add_row("  main P&L", f"[{main_style}]${main_pnl:+,.2f}[/{main_style}]")
    table.add_row("  short_horizon P&L", f"[{short_style}]${short_pnl:+,.2f}[/{short_style}]")
    table.add_row("ROI", f"{roi:+.2f}%")
    table.add_row("Avg P&L/Trade", f"${avg_pnl:+,.2f}")
    table.add_row("Sharpe Ratio", f"{sharpe:.2f}")

    console.print(table)

    if daily:
        with db.cursor() as cur:
            cur.execute("""
                SELECT DATE(resolved_at) AS day,
                       COUNT(*)          AS trades,
                       SUM(pnl)          AS day_pnl
                FROM polyagent_trades_v
                GROUP BY DATE(resolved_at)
                ORDER BY day DESC LIMIT 14
            """)
            days = cur.fetchall()

        day_table = Table(title="Daily P&L (combined)")
        day_table.add_column("Date", style="cyan")
        day_table.add_column("Trades", justify="right")
        day_table.add_column("P&L", justify="right")
        for d in days:
            dpnl = float(d["day_pnl"])
            s = "green" if dpnl >= 0 else "red"
            day_table.add_row(str(d["day"]), str(d["trades"]), f"[{s}]${dpnl:+,.2f}[/{s}]")
        console.print(day_table)

    db.close()

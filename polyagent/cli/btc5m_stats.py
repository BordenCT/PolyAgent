"""BTC 5m paper-trading analytics — `polyagent btc5m-stats`."""
from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

from polyagent.infra.config import Settings
from polyagent.infra.database import Database


STATS_QUERY_TOTAL = """
    SELECT
        COUNT(*)                                                   AS trades,
        COUNT(*) FILTER (WHERE pnl > 0)                            AS wins,
        COUNT(*) FILTER (WHERE pnl <= 0)                           AS losses,
        COALESCE(AVG(edge_at_decision), 0)                         AS avg_edge,
        COALESCE(SUM(pnl), 0)                                      AS total_pnl,
        COALESCE(AVG(pnl), 0)                                      AS avg_pnl,
        COALESCE(AVG(vol_at_decision), 0)                          AS avg_vol
    FROM btc5m_trades
    WHERE pnl IS NOT NULL
"""

STATS_QUERY_BY_DURATION = """
    SELECT
        m.window_duration_s                                        AS window_duration_s,
        COUNT(*)                                                   AS trades,
        COUNT(*) FILTER (WHERE t.pnl > 0)                          AS wins,
        COUNT(*) FILTER (WHERE t.pnl <= 0)                         AS losses,
        COALESCE(AVG(t.edge_at_decision), 0)                       AS avg_edge,
        COALESCE(SUM(t.pnl), 0)                                    AS total_pnl,
        COALESCE(AVG(t.pnl), 0)                                    AS avg_pnl
    FROM btc5m_trades t
    JOIN btc5m_markets m ON m.id = t.market_id
    WHERE t.pnl IS NOT NULL
    GROUP BY m.window_duration_s
    ORDER BY m.window_duration_s
"""


def _fmt_duration(seconds: int) -> str:
    """Format a duration in seconds to a human-readable string (e.g. 300 → '5m')."""
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


@click.command("btc5m-stats")
@click.option("--by-duration", is_flag=True,
              help="Break out performance per window duration (5m vs 15m vs ...).")
def btc5m_stats(by_duration: bool):
    """Paper-trading performance of the BTC short-horizon subsystem."""
    console = Console()
    settings = Settings.from_env()
    db = Database(settings)

    if by_duration:
        with db.cursor() as cur:
            cur.execute(STATS_QUERY_BY_DURATION)
            rows = cur.fetchall()

        table = Table(title="BTC Up/Down Performance by Timeframe")
        table.add_column("Window", style="cyan")
        table.add_column("Trades", justify="right")
        table.add_column("W/L", justify="right")
        table.add_column("Win%", justify="right")
        table.add_column("Avg Edge", justify="right")
        table.add_column("Avg P&L", justify="right")
        table.add_column("Total P&L", justify="right")

        if not rows:
            table.add_row("(none)", "0", "-", "-", "-", "-", "$0.00")
        else:
            for r in rows:
                trades = int(r["trades"])
                wins = int(r["wins"])
                losses = int(r["losses"])
                win_pct = (wins / trades * 100) if trades else 0.0
                total_pnl = float(r["total_pnl"])
                avg_pnl = float(r["avg_pnl"])
                avg_edge = float(r["avg_edge"])
                pnl_style = "green" if total_pnl >= 0 else "red"
                table.add_row(
                    _fmt_duration(int(r["window_duration_s"])),
                    str(trades),
                    f"{wins}/{losses}",
                    f"{win_pct:.1f}%",
                    f"{avg_edge:+.3f}",
                    f"${avg_pnl:+,.2f}",
                    f"[{pnl_style}]${total_pnl:+,.2f}[/{pnl_style}]",
                )

        console.print(table)
        db.close()
        return

    with db.cursor() as cur:
        cur.execute(STATS_QUERY_TOTAL)
        row = cur.fetchone()

    trades = int(row["trades"] or 0)
    wins = int(row["wins"] or 0)
    losses = int(row["losses"] or 0)
    avg_edge = float(row["avg_edge"] or 0)
    total_pnl = float(row["total_pnl"] or 0)
    avg_pnl = float(row["avg_pnl"] or 0)
    avg_vol = float(row["avg_vol"] or 0)

    table = Table(title="BTC Up/Down Paper-Trading Performance")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")

    if trades == 0:
        table.add_row("Trades", "0")
        console.print(table)
        db.close()
        return

    win_pct = (wins / trades) * 100
    pnl_style = "green" if total_pnl >= 0 else "red"
    table.add_row("Trades", str(trades))
    table.add_row("W/L", f"{wins}/{losses}")
    table.add_row("Win%", f"{win_pct:.1f}%")
    table.add_row("Avg Edge", f"{avg_edge:+.3f}")
    table.add_row("Avg P&L", f"${avg_pnl:+,.2f}")
    table.add_row("Total P&L", f"[{pnl_style}]${total_pnl:+,.2f}[/{pnl_style}]")
    table.add_row("Avg Realized Vol", f"{avg_vol:.3f}")

    console.print(table)
    db.close()

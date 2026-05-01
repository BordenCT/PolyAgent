"""Quant short-horizon paper-trading analytics: `polyagent quant-stats`."""
from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

from polyagent.infra.config import Settings
from polyagent.infra.database import Database


# All queries read from the `quant_short_v` view (see migration 007), which
# pre-joins quant_short_trades + quant_short_markets and exposes derived
# fields (`abs_edge`, `won`, `window_minutes`). Adding a new analytic just
# means selecting from the view, not modifying tables or repeating joins.
STATS_QUERY_TOTAL = """
    SELECT
        COUNT(*)                            AS trades,
        COUNT(*) FILTER (WHERE won)         AS wins,
        COUNT(*) FILTER (WHERE NOT won)     AS losses,
        COALESCE(AVG(abs_edge), 0)          AS avg_edge,
        COALESCE(SUM(pnl), 0)               AS total_pnl,
        COALESCE(AVG(pnl), 0)               AS avg_pnl,
        COALESCE(AVG(vol_at_decision), 0)   AS avg_vol
    FROM quant_short_v
    WHERE pnl IS NOT NULL
      AND (%(asset)s::text IS NULL OR asset_id = %(asset)s)
"""

STATS_QUERY_BY_DURATION = """
    SELECT
        window_duration_s,
        COUNT(*)                            AS trades,
        COUNT(*) FILTER (WHERE won)         AS wins,
        COUNT(*) FILTER (WHERE NOT won)     AS losses,
        COALESCE(AVG(abs_edge), 0)          AS avg_edge,
        COALESCE(SUM(pnl), 0)               AS total_pnl,
        COALESCE(AVG(pnl), 0)               AS avg_pnl
    FROM quant_short_v
    WHERE pnl IS NOT NULL
      AND (%(asset)s::text IS NULL OR asset_id = %(asset)s)
    GROUP BY window_duration_s
    ORDER BY window_duration_s
"""

STATS_QUERY_BY_ASSET = """
    SELECT
        asset_id,
        COUNT(*)                            AS trades,
        COUNT(*) FILTER (WHERE won)         AS wins,
        COUNT(*) FILTER (WHERE NOT won)     AS losses,
        COALESCE(AVG(abs_edge), 0)          AS avg_edge,
        COALESCE(SUM(pnl), 0)               AS total_pnl,
        COALESCE(AVG(pnl), 0)               AS avg_pnl
    FROM quant_short_v
    WHERE pnl IS NOT NULL
    GROUP BY asset_id
    ORDER BY asset_id
"""


def _fmt_duration(seconds: int) -> str:
    """Format a duration in seconds to a human-readable string (e.g. 300 -> '5m')."""
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _render_breakdown(console: Console, rows, title: str, key_col: str, key_fmt) -> None:
    table = Table(title=title)
    table.add_column(key_col, style="cyan")
    table.add_column("Trades", justify="right")
    table.add_column("W/L", justify="right")
    table.add_column("Win%", justify="right")
    table.add_column("Avg |Edge|", justify="right")
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
                key_fmt(r),
                str(trades),
                f"{wins}/{losses}",
                f"{win_pct:.1f}%",
                f"{avg_edge:.4f}",
                f"${avg_pnl:+,.2f}",
                f"[{pnl_style}]${total_pnl:+,.2f}[/{pnl_style}]",
            )
    console.print(table)


@click.command("quant-stats")
@click.option("--asset", type=str, default=None,
              help="Filter to a single asset_id (e.g. BTC). Default: all assets.")
@click.option("--by-duration", is_flag=True,
              help="Break out performance per window duration (5m vs 15m vs ...).")
@click.option("--by-asset", is_flag=True,
              help="Break out performance per asset_id.")
def quant_stats(asset: str | None, by_duration: bool, by_asset: bool) -> None:
    """Paper-trading performance of the quant short-horizon subsystem."""
    console = Console()
    settings = Settings.from_env()
    db = Database(settings)

    try:
        if by_asset:
            with db.cursor() as cur:
                cur.execute(STATS_QUERY_BY_ASSET)
                rows = cur.fetchall()
            _render_breakdown(
                console, rows,
                title="Quant Up/Down Performance by Asset",
                key_col="Asset",
                key_fmt=lambda r: r["asset_id"],
            )
            return

        if by_duration:
            with db.cursor() as cur:
                cur.execute(STATS_QUERY_BY_DURATION, {"asset": asset})
                rows = cur.fetchall()
            title = (
                f"Quant Up/Down Performance by Timeframe ({asset})"
                if asset else "Quant Up/Down Performance by Timeframe"
            )
            _render_breakdown(
                console, rows,
                title=title,
                key_col="Window",
                key_fmt=lambda r: _fmt_duration(int(r["window_duration_s"])),
            )
            return

        with db.cursor() as cur:
            cur.execute(STATS_QUERY_TOTAL, {"asset": asset})
            row = cur.fetchone()

        trades = int(row["trades"] or 0)
        wins = int(row["wins"] or 0)
        losses = int(row["losses"] or 0)
        avg_edge = float(row["avg_edge"] or 0)
        total_pnl = float(row["total_pnl"] or 0)
        avg_pnl = float(row["avg_pnl"] or 0)
        avg_vol = float(row["avg_vol"] or 0)

        title = (
            f"Quant Up/Down Paper-Trading Performance ({asset})"
            if asset else "Quant Up/Down Paper-Trading Performance"
        )
        table = Table(title=title)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right")

        if trades == 0:
            table.add_row("Trades", "0")
            console.print(table)
            return

        win_pct = (wins / trades) * 100
        pnl_style = "green" if total_pnl >= 0 else "red"
        table.add_row("Trades", str(trades))
        table.add_row("W/L", f"{wins}/{losses}")
        table.add_row("Win%", f"{win_pct:.1f}%")
        table.add_row("Avg |Edge|", f"{avg_edge:.4f}")
        table.add_row("Avg P&L", f"${avg_pnl:+,.2f}")
        table.add_row("Total P&L", f"[{pnl_style}]${total_pnl:+,.2f}[/{pnl_style}]")
        table.add_row("Avg Realized Vol", f"{avg_vol:.3f}")

        console.print(table)
    finally:
        db.close()

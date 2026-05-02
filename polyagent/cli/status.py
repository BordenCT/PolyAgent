"""Status command — live bot state."""
from __future__ import annotations

import time
from decimal import Decimal

import click
from rich.console import Console
from rich.table import Table

from polyagent.infra.config import Settings
from polyagent.infra.database import Database
from polyagent.services.bankroll import compute_bankroll_state


@click.command()
@click.option("--watch", is_flag=True, help="Auto-refresh every 5 seconds")
def status(watch: bool):
    """Show current bot status: workers, queue depths, uptime."""
    console = Console()

    def render():
        try:
            settings = Settings.from_env()
            db = Database(settings)

            with db.cursor() as cur:
                cur.execute("SELECT status, COUNT(*) as cnt FROM markets GROUP BY status")
                market_counts = {r["status"]: r["cnt"] for r in cur.fetchall()}

                cur.execute("SELECT COUNT(*) as cnt FROM positions WHERE status = 'open'")
                open_positions = cur.fetchone()["cnt"]

                cur.execute("SELECT COUNT(*) as cnt FROM thesis WHERE created_at > NOW() - INTERVAL '24 hours'")
                recent_theses = cur.fetchone()["cnt"]

                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE pnl IS NOT NULL)            AS resolved,
                        COUNT(*) FILTER (WHERE pnl IS NULL)                AS open,
                        COALESCE(AVG(size) FILTER (WHERE pnl IS NULL), 0)  AS avg_open_size,
                        COALESCE(AVG(size) FILTER (WHERE pnl IS NOT NULL), 0) AS avg_resolved_size
                    FROM quant_short_trades
                """)
                qs_row = cur.fetchone()
                qs_resolved = int(qs_row["resolved"] or 0)
                qs_open = int(qs_row["open"] or 0)
                qs_avg_open = Decimal(str(qs_row["avg_open_size"] or 0))
                qs_avg_resolved = Decimal(str(qs_row["avg_resolved_size"] or 0))

            # Unified bankroll across both ledgers.
            bk = compute_bankroll_state(db, settings.bankroll)
            db.close()

            table = Table(title="PolyAgent Status")
            table.add_column("Metric", style="cyan")
            table.add_column("Value", style="green")

            table.add_row("Mode", "PAPER" if settings.paper_trade else "LIVE")
            table.add_row("Scan Interval", f"{settings.scan_interval_hours}h")
            table.add_row("Markets Queued", str(market_counts.get("queued", 0)))
            table.add_row("Markets Evaluating", str(market_counts.get("evaluating", 0)))
            table.add_row("Markets Traded", str(market_counts.get("traded", 0)))
            table.add_row("Markets Rejected", str(market_counts.get("rejected", 0)))
            table.add_row("Open Positions (main)", str(open_positions))
            table.add_row("Open Positions (short)", str(qs_open))
            table.add_row("Theses (24h)", str(recent_theses))
            table.add_row("Starting Bankroll", f"${float(bk.starting):,.2f}")
            table.add_row("Realized P&L (main)", _colorize_pnl(bk.realized_main))
            table.add_row("Realized P&L (short)", _colorize_pnl(bk.realized_quant))
            table.add_row("Realized P&L (total)", _colorize_pnl(bk.realized_total))
            table.add_row("Open Capital (main)", f"${float(bk.open_capital_main):,.2f}")
            table.add_row("Open Capital (short)", f"${float(bk.open_capital_quant):,.2f}")
            table.add_row("Open Capital (total)", f"${float(bk.open_capital_total):,.2f}")
            table.add_row(
                "Free Bankroll (unified)",
                _free_bankroll_display(bk.free, settings.min_free_bankroll),
            )
            table.add_row(
                "Avg Open Bet (short)",
                f"${float(qs_avg_open):.2f}" if qs_open else "-",
            )
            table.add_row(
                "Avg Resolved Bet (short)",
                f"${float(qs_avg_resolved):.2f}" if qs_resolved else "-",
            )
            table.add_row(
                "Quant Short Resolved",
                str(qs_resolved),
            )

            console.clear()
            console.print(table)
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")

    render()
    if watch:
        while True:
            time.sleep(5)
            render()


def _colorize_pnl(pnl: Decimal) -> str:
    val = float(pnl)
    if val > 0:
        return f"[green]${val:+,.2f}[/green]"
    if val < 0:
        return f"[red]${val:+,.2f}[/red]"
    return f"${val:+,.2f}"


def _free_bankroll_display(free: Decimal, floor: float) -> str:
    val = float(free)
    formatted = f"${val:,.2f}"
    if val < floor:
        return f"[red]{formatted} (below ${floor:.2f} floor — no new trades)[/red]"
    return formatted

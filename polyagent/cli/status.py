"""Status command — live bot state."""
from __future__ import annotations

import time
from decimal import Decimal

import click
from rich.console import Console
from rich.table import Table

from polyagent.data.repositories.positions import PositionRepository
from polyagent.infra.config import Settings
from polyagent.infra.database import Database


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

                # Short-horizon quant subsystem (separate ledger; unifies in Phase 2).
                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE pnl IS NOT NULL) AS resolved,
                        COALESCE(SUM(pnl), 0)                   AS total_pnl
                    FROM quant_short_trades
                """)
                qs_row = cur.fetchone()
                qs_resolved = int(qs_row["resolved"] or 0)
                qs_pnl = Decimal(str(qs_row["total_pnl"] or 0))

            position_repo = PositionRepository(db)
            open_capital, realized_pnl = position_repo.get_capital_state()
            starting = Decimal(str(settings.bankroll))
            free_bankroll = starting + realized_pnl - open_capital
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
            table.add_row("Open Positions", str(open_positions))
            table.add_row("Theses (24h)", str(recent_theses))
            table.add_row("Starting Bankroll", f"${float(starting):,.2f}")
            table.add_row("Realized P&L", _colorize_pnl(realized_pnl))
            table.add_row("Open Capital", f"${float(open_capital):,.2f}")
            table.add_row(
                "Free Bankroll",
                _free_bankroll_display(free_bankroll, settings.min_free_bankroll),
            )
            table.add_row(
                "Quant Short (paper)",
                f"{qs_resolved} resolved, {_colorize_pnl(qs_pnl)}",
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

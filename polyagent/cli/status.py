"""Status command — live bot state."""
from __future__ import annotations

import time

import click
from rich.console import Console
from rich.table import Table

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
            table.add_row("Bankroll", f"${settings.bankroll:,.2f}")

            console.clear()
            console.print(table)
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")

    render()
    if watch:
        while True:
            time.sleep(5)
            render()

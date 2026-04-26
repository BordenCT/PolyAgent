"""Positions command — view open and closed positions."""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import click
from rich.console import Console
from rich.table import Table

from polyagent.infra.config import Settings
from polyagent.infra.database import Database
from polyagent.data.repositories.positions import PositionRepository


def _json_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    raise TypeError(f"Type {type(obj).__name__} not JSON serializable")


@click.command()
@click.option("--closed", is_flag=True, help="Show closed positions")
@click.option("--worst", is_flag=True, help="Show worst-performing positions")
@click.option("--limit", type=int, default=20, show_default=True, help="Max rows to fetch (closed only)")
@click.option("--all", "all_rows", is_flag=True, help="Fetch all closed positions (overrides --limit)")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON to stdout instead of a table")
def positions(closed: bool, worst: bool, limit: int, all_rows: bool, as_json: bool):
    """Show positions. Default: open positions with current P&L."""
    console = Console()
    settings = Settings.from_env()
    db = Database(settings)
    repo = PositionRepository(db)

    if closed or worst:
        rows = repo.get_closed(limit=None if all_rows else limit)
        if worst:
            rows = sorted(rows, key=lambda r: float(r.get("pnl", 0)))
        title = "Worst Positions" if worst else "Closed Positions"
    else:
        rows = repo.get_open()
        title = "Open Positions"

    if as_json:
        click.echo(json.dumps(list(rows), default=_json_default, indent=2))
        db.close()
        return

    table = Table(title=f"{title} ({len(rows)})")
    table.add_column("ID", style="dim", max_width=8)
    table.add_column("Market", max_width=40)
    table.add_column("Side", style="cyan")
    table.add_column("Entry", justify="right")
    table.add_column("Current", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("P&L", justify="right")
    if closed or worst:
        table.add_column("Exit", style="yellow")

    for r in rows:
        pnl = float(r.get("pnl", 0))
        pnl_style = "green" if pnl >= 0 else "red"
        row = [
            str(r["id"])[:8],
            r.get("question", str(r.get("market_id", ""))[:8]),
            r.get("side", "?"),
            f"${float(r['entry_price']):.4f}",
            f"${float(r['current_price']):.4f}",
            f"${float(r['position_size']):.2f}",
            f"[{pnl_style}]${pnl:+.2f}[/{pnl_style}]",
        ]
        if closed or worst:
            row.append(r.get("exit_reason", "N/A"))
        table.add_row(*row)

    console.print(table)
    db.close()

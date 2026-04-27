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


def _print_summary(console: Console, rows: list[dict]) -> None:
    """Print aggregate rollups for closed positions: totals, by side, by exit_reason, by class."""
    if not rows:
        console.print("[yellow]No closed positions.[/yellow]")
        return

    def _agg(items: list[dict]) -> tuple[int, int, float, float]:
        n = len(items)
        wins = sum(1 for r in items if float(r.get("pnl", 0)) > 0)
        total = sum(float(r.get("pnl", 0)) for r in items)
        staked = sum(float(r.get("position_size", 0)) for r in items)
        return n, wins, total, staked

    n, wins, total, staked = _agg(rows)
    roi = (total / staked * 100) if staked else 0.0
    head = Table(title=f"Closed Positions Summary (n={n})")
    head.add_column("Metric"); head.add_column("Value", justify="right")
    head.add_row("Trades", str(n))
    head.add_row("Winners", f"{wins} ({wins / n * 100:.1f}%)")
    head.add_row("Total P&L", f"${total:+.2f}")
    head.add_row("Total Staked", f"${staked:.2f}")
    head.add_row("ROI", f"{roi:+.1f}%")
    console.print(head)

    def _group(key: str) -> Table:
        buckets: dict[str, list[dict]] = {}
        for r in rows:
            buckets.setdefault(str(r.get(key) or "?"), []).append(r)
        t = Table(title=f"By {key}")
        t.add_column(key); t.add_column("N", justify="right"); t.add_column("W%", justify="right")
        t.add_column("P&L", justify="right"); t.add_column("Staked", justify="right"); t.add_column("ROI", justify="right")
        for k, items in sorted(buckets.items(), key=lambda kv: -sum(float(r.get("pnl", 0)) for r in kv[1])):
            bn, bw, bp, bs = _agg(items)
            broi = (bp / bs * 100) if bs else 0.0
            t.add_row(k, str(bn), f"{bw / bn * 100:.0f}%", f"${bp:+.2f}", f"${bs:.2f}", f"{broi:+.1f}%")
        return t

    console.print(_group("side"))
    console.print(_group("exit_reason"))
    console.print(_group("market_class"))


@click.command()
@click.option("--closed", is_flag=True, help="Show closed positions")
@click.option("--worst", is_flag=True, help="Show worst-performing positions")
@click.option("--limit", type=int, default=20, show_default=True, help="Max rows to fetch (closed only)")
@click.option("--all", "all_rows", is_flag=True, help="Fetch all closed positions (overrides --limit)")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON to stdout instead of a table")
@click.option("--summary", is_flag=True, help="Print aggregate rollups instead of rows (closed only)")
def positions(closed: bool, worst: bool, limit: int, all_rows: bool, as_json: bool, summary: bool):
    """Show positions. Default: open positions with current P&L."""
    console = Console()
    settings = Settings.from_env()
    db = Database(settings)
    repo = PositionRepository(db)

    if closed or worst or summary:
        rows = repo.get_closed(limit=None if (all_rows or summary) else limit)
        if worst:
            rows = sorted(rows, key=lambda r: float(r.get("pnl", 0)))
        title = "Worst Positions" if worst else "Closed Positions"
    else:
        rows = repo.get_open()
        title = "Open Positions"

    if summary:
        _print_summary(console, list(rows))
        db.close()
        return

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

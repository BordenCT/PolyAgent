"""Markets command — view scanned markets and theses."""
from __future__ import annotations

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from polyagent.infra.config import Settings
from polyagent.infra.database import Database


@click.command()
@click.option("--rejected", is_flag=True, help="Show rejected markets")
def markets(rejected: bool):
    """Show current market queue with IDs and scores.

    Use market IDs with 'polyagent thesis <MARKET_ID>' to inspect a thesis.
    """
    console = Console()
    settings = Settings.from_env()
    db = Database(settings)

    status_filter = "rejected" if rejected else "queued"
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT id, polymarket_id, question, category, score, status, scanned_at
            FROM markets WHERE status = %(status)s
            ORDER BY scanned_at DESC LIMIT 50
            """,
            {"status": status_filter},
        )
        rows = cur.fetchall()

    title = "Rejected Markets" if rejected else "Market Queue"
    table = Table(title=title)
    table.add_column("ID", style="dim", max_width=8)
    table.add_column("Market", max_width=50)
    table.add_column("Category", style="cyan")
    table.add_column("Score", justify="right")
    table.add_column("Status", style="yellow")

    for r in rows:
        score = r.get("score", {})
        ev = score.get("ev", 0) if isinstance(score, dict) else 0
        table.add_row(
            str(r["id"])[:8],
            r["question"][:50],
            r["category"],
            f"{ev:.3f}" if ev else "—",
            r["status"],
        )

    console.print(table)
    console.print(
        "\n[dim]Tip: run 'polyagent thesis <ID>' to see the full thesis for a market[/dim]"
    )
    db.close()


@click.command()
@click.argument("market_id")
def thesis(market_id: str):
    """Show the full thesis and check details for a market.

    MARKET_ID: First 8 chars of the market UUID (from 'polyagent markets').
    """
    console = Console()
    settings = Settings.from_env()
    db = Database(settings)

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT t.*, m.question, m.polymarket_id, m.midpoint_price
            FROM thesis t
            JOIN markets m ON t.market_id = m.id
            WHERE CAST(m.id AS TEXT) LIKE %(prefix)s
            ORDER BY t.created_at DESC LIMIT 1
            """,
            {"prefix": f"{market_id}%"},
        )
        row = cur.fetchone()

    if not row:
        console.print(f"[red]No thesis found for market ID starting with '{market_id}'[/red]")
        console.print("[dim]Run 'polyagent markets' to see available IDs[/dim]")
        db.close()
        return

    checks = row.get("checks", {})
    panel_text = (
        f"[bold]{row['question']}[/bold]\n"
        f"Polymarket ID: {row['polymarket_id']}\n"
        f"Market Price: {row['midpoint_price']}\n\n"
        f"[cyan]Claude Estimate:[/cyan] {float(row['claude_estimate']):.2%}\n"
        f"[cyan]Confidence:[/cyan] {float(row['confidence']):.2%}\n"
        f"[cyan]Checks Passed:[/cyan] {row['checks_passed']}/4\n\n"
        f"  Base Rate: {'[green]PASS[/green]' if checks.get('base_rate') else '[red]FAIL[/red]'}\n"
        f"  News:      {'[green]PASS[/green]' if checks.get('news') else '[red]FAIL[/red]'}\n"
        f"  Whale:     {'[green]PASS[/green]' if checks.get('whale') else '[red]FAIL[/red]'}\n"
        f"  Disposition: {'[green]PASS[/green]' if checks.get('disposition') else '[red]FAIL[/red]'}\n\n"
        f"[cyan]Consensus:[/cyan] {row['consensus']}\n"
        f"[cyan]Strategy Votes:[/cyan] {row.get('strategy_votes', {})}\n\n"
        f"[bold]Thesis:[/bold]\n{row['thesis_text']}"
    )

    console.print(Panel(panel_text, title="Market Thesis", expand=False))
    db.close()

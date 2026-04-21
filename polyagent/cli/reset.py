"""Reset command — wipe paper trading state while preserving learned priors."""
from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

from polyagent.infra.config import Settings
from polyagent.infra.database import Database

TABLES_TO_WIPE = ("trade_log", "positions", "thesis", "markets")
TABLES_TO_PRESERVE = ("historical_outcomes", "target_wallets")


@click.command()
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt")
def reset(yes: bool):
    """Wipe paper trading state (markets/thesis/positions/trade_log) and restart fresh.

    Preserves historical_outcomes and target_wallets so learned priors and
    seed data survive. Uses DATABASE_URL from .env.
    """
    console = Console()
    settings = Settings.from_env()
    db = Database(settings)

    before = _count_rows(db, TABLES_TO_WIPE + TABLES_TO_PRESERVE)
    _print_counts(console, "Current state", before)

    total_to_wipe = sum(before.get(t, 0) for t in TABLES_TO_WIPE)
    if total_to_wipe == 0:
        console.print("[green]Nothing to wipe — paper tables already empty.[/green]")
        db.close()
        return

    console.print(f"[yellow]DATABASE_URL:[/yellow] {settings.database_url}")
    console.print(
        f"[yellow]About to TRUNCATE {', '.join(TABLES_TO_WIPE)} "
        f"({total_to_wipe:,} rows).[/yellow]"
    )

    if not yes and not click.confirm("Proceed?", default=False):
        console.print("[red]Aborted.[/red]")
        db.close()
        return

    with db.cursor() as cur:
        for table in TABLES_TO_WIPE:
            cur.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE")

    after = _count_rows(db, TABLES_TO_WIPE + TABLES_TO_PRESERVE)
    db.close()

    _print_counts(console, "After reset", after)
    console.print("[green]Reset complete.[/green]")


def _count_rows(db: Database, tables: tuple[str, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    with db.cursor() as cur:
        for table in tables:
            cur.execute(f"SELECT COUNT(*) AS cnt FROM {table}")
            row = cur.fetchone()
            counts[table] = int(row["cnt"]) if row else 0
    return counts


def _print_counts(console: Console, title: str, counts: dict[str, int]) -> None:
    table = Table(title=title)
    table.add_column("Table", style="cyan")
    table.add_column("Rows", style="green", justify="right")
    table.add_column("Action", style="yellow")
    for name in TABLES_TO_WIPE:
        table.add_row(name, f"{counts.get(name, 0):,}", "WIPE")
    for name in TABLES_TO_PRESERVE:
        table.add_row(name, f"{counts.get(name, 0):,}", "PRESERVE")
    console.print(table)

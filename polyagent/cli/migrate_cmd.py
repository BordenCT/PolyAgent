"""`polyagent migrate` subcommand: up / status / baseline."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import click
import psycopg

from polyagent.scripts.migrate import (
    DriftError,
    migrate_baseline,
    migrate_status,
    migrate_up,
)

_DEFAULT_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"


def _connect() -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        click.echo("DATABASE_URL not set", err=True)
        sys.exit(2)
    return psycopg.connect(url)


@click.group()
def migrate() -> None:
    """Database migration runner."""


@migrate.command("up")
@click.option("--dir", "directory", type=click.Path(path_type=Path),
              default=_DEFAULT_DIR, show_default=True,
              help="Migrations directory.")
def up_cmd(directory: Path) -> None:
    """Apply pending migrations."""
    try:
        with _connect() as conn:
            applied = migrate_up(conn, directory)
    except DriftError as exc:
        click.echo(f"DRIFT: {exc}", err=True)
        sys.exit(1)
    if not applied:
        click.echo("Nothing to apply.")
        return
    for m in applied:
        click.echo(f"applied {m.version} {m.filename}")


@migrate.command("status")
@click.option("--dir", "directory", type=click.Path(path_type=Path),
              default=_DEFAULT_DIR, show_default=True)
def status_cmd(directory: Path) -> None:
    """Show applied / pending / drifted migrations."""
    with _connect() as conn:
        report = migrate_status(conn, directory)
    click.echo("Applied:")
    for m in report.applied:
        click.echo(f"  {m.version} {m.filename}")
    click.echo("Pending:")
    for m in report.pending:
        click.echo(f"  {m.version} {m.filename}")
    click.echo("Drifted:")
    for m in report.drifted:
        click.echo(f"  {m.version} {m.filename}")
    if report.drifted:
        sys.exit(1)


@migrate.command("baseline")
@click.option("--dir", "directory", type=click.Path(path_type=Path),
              default=_DEFAULT_DIR, show_default=True)
def baseline_cmd(directory: Path) -> None:
    """Record all migration files as applied without executing them."""
    with _connect() as conn:
        recorded = migrate_baseline(conn, directory)
    if not recorded:
        click.echo("Nothing to baseline.")
        return
    for m in recorded:
        click.echo(f"baselined {m.version} {m.filename}")

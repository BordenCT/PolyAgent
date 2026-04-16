"""Click CLI group entry point."""
from __future__ import annotations

import click

from polyagent.cli.markets import markets, thesis
from polyagent.cli.performance import perf
from polyagent.cli.positions import positions
from polyagent.cli.status import status


@click.group()
@click.version_option(version="0.1.0", prog_name="polyagent")
def cli():
    """PolyAgent -- Autonomous Polymarket trading bot.

    Use 'polyagent <command> --help' for details on each command.
    """
    pass


cli.add_command(status)
cli.add_command(perf)
cli.add_command(positions)
cli.add_command(markets)
cli.add_command(thesis)

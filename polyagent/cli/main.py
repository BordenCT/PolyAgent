"""Click CLI group entry point."""
from __future__ import annotations

import click

from polyagent.cli.backtest_cmd import backtest
from polyagent.cli.class_stats import class_stats
from polyagent.cli.ingest_cmd import ingest
from polyagent.cli.markets import markets, thesis
from polyagent.cli.performance import perf
from polyagent.cli.positions import positions
from polyagent.cli.reset import reset
from polyagent.cli.status import status


@click.group()
@click.version_option(version="0.1.0", prog_name="polyagent")
def cli():
    """PolyAgent -- Autonomous Polymarket trading bot.

    Use 'polyagent <command> --help' for details on each command.
    """
    pass


@cli.command()
def run():
    """Start the trading bot (paper or live depending on .env)."""
    from polyagent.main import run as _run
    _run()


cli.add_command(status)
cli.add_command(perf)
cli.add_command(positions)
cli.add_command(markets)
cli.add_command(thesis)
cli.add_command(backtest)
cli.add_command(ingest)
cli.add_command(reset)
cli.add_command(class_stats)

"""Data ingestion CLI command."""
from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from polyagent.scripts.ingest import DataIngester


@click.command()
@click.option("--snapshot", is_flag=True, help="Download pre-built snapshot (~2GB) instead of scraping")
@click.option("--full", is_flag=True, help="Full scrape from Goldsky subgraph (slow, 2+ days first run)")
@click.option("--process", "process_only", is_flag=True, help="Just re-process existing orderFilled.csv into trades")
@click.option("--candles", is_flag=True, help="Fetch OHLC price history for all markets via CLOB API")
@click.option("--since", default=None, help="Fetch candles since this date (YYYY-MM-DD), e.g. --since 2024-01-01")
@click.option("--data-dir", type=click.Path(), default="./data", help="Directory to store ingested data")
def ingest(snapshot: bool, full: bool, process_only: bool, candles: bool, since: str | None, data_dir: str):
    """Fetch and process Polymarket historical data.

    Data is stored locally and used by the backtest engine.

    \b
    Modes:
      --snapshot   Download pre-built snapshot (~2GB, fast)
      --full       Scrape from Goldsky subgraph (complete, slow)
      --process    Re-process existing raw data into trades
      --candles    Fetch OHLC price history for all markets via CLOB API

    \b
    Examples:
      polyagent ingest --snapshot                    # Fastest: download + process
      polyagent ingest --snapshot --data-dir ~/data  # Custom directory
      polyagent ingest --full                        # Full scrape (2+ days first run)
      polyagent ingest --process                     # Re-process after fixing something
      polyagent ingest --candles                     # Fetch candles for all 49,971 markets
      polyagent ingest --candles --since 2024-01-01  # Candles from a specific date
    """
    console = Console()
    ingester = DataIngester(data_dir)

    if candles:
        console.print("[cyan]Fetching OHLC candles for all markets...[/cyan]")
        # Ensure markets.csv exists first.
        if not ingester.markets_csv.exists():
            console.print("[cyan]Fetching market metadata first...[/cyan]")
            ingester.fetch_markets()
        try:
            count = ingester.fetch_candles(since=since)
            console.print(f"[green]Fetched {count:,} hourly bars -> {ingester.candles_csv}[/green]")
            console.print(f"\n[bold green]Done![/bold green]")
            console.print(f"[dim]Backtest with: polyagent backtest --data-dir {data_dir}[/dim]")
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
            raise
        finally:
            ingester.close()
        return

    if not any([snapshot, full, process_only]):
        console.print("[yellow]No mode specified. Use --snapshot (recommended), --full, or --process[/yellow]")
        console.print("[dim]Run 'polyagent ingest --help' for details[/dim]")
        return

    try:
        # Stage 1: Markets (always needed)
        if not process_only:
            console.print("[cyan]Stage 1/3: Fetching market metadata...[/cyan]")
            count = ingester.fetch_markets()
            console.print(f"[green]Markets: {count} total[/green]")

        # Stage 2: Order events
        if snapshot:
            console.print("[cyan]Stage 2/3: Downloading snapshot...[/cyan]")
            if ingester.download_snapshot():
                console.print("[green]Snapshot ready[/green]")
            else:
                console.print("[red]Snapshot download failed. Try --full instead.[/red]")
                return
        elif full:
            console.print("[cyan]Stage 2/3: Scraping Goldsky (this will take a while)...[/cyan]")
            count = ingester.scrape_goldsky()
            console.print(f"[green]Scraped {count} order events[/green]")

        # Stage 3: Process
        console.print("[cyan]Stage 3/3: Processing trades...[/cyan]")
        count = ingester.process_trades()
        console.print(f"[green]Processed {count:,} trades -> {ingester.trades_csv}[/green]")

        console.print(f"\n[bold green]Ingestion complete![/bold green]")
        console.print(f"[dim]Backtest with: polyagent backtest --data-dir {data_dir}[/dim]")

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise
    finally:
        ingester.close()

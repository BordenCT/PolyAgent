"""Load trade data into daily market snapshots for backtesting.

Handles datasets larger than RAM by aggregating in chunks via Polars
batched reader — only the daily summaries stay in memory.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import polars as pl
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from polyagent.models import MarketData

logger = logging.getLogger("polyagent.backtest.data_loader")


@dataclass
class MarketSnapshot:
    """A single market's daily state — aggregated from raw trades."""

    polymarket_id: str
    question: str
    category: str
    token_id: str
    price: Decimal
    volume: Decimal
    timestamp: datetime
    outcome: str | None = None

    def to_market_data(self, hours_to_resolution: float = 48.0) -> MarketData:
        """Convert to MarketData for the scanner."""
        return MarketData(
            polymarket_id=self.polymarket_id,
            question=self.question,
            category=self.category,
            token_id=self.token_id,
            midpoint_price=self.price,
            bids_depth=self.volume,
            asks_depth=self.volume,
            hours_to_resolution=hours_to_resolution,
            volume_24h=self.volume,
        )

    @staticmethod
    def parse_trade_row(row: dict) -> MarketSnapshot:
        """Parse a single trade row from processed trades CSV."""
        ts_str = row.get("timestamp", "")
        if ts_str:
            try:
                ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            except ValueError:
                ts = datetime.now(timezone.utc)
        else:
            ts = datetime.now(timezone.utc)

        return MarketSnapshot(
            polymarket_id=row.get("market_id", row.get("condition_id", "")),
            question=row.get("question", ""),
            category=row.get("category", "unknown"),
            token_id=row.get("token_id", ""),
            price=Decimal(str(row.get("price", 0))),
            volume=Decimal(str(row.get("usd_amount", row.get("size", 0)))),
            timestamp=ts,
            outcome=row.get("outcome"),
        )


class DataLoader:
    """Loads and organizes historical market data for backtesting.

    Processes large CSVs in chunks — never loads the full file into memory.
    Aggregates to daily market-level summaries for the backtest engine.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)

    @staticmethod
    def parse_trade_row(row: dict) -> MarketSnapshot:
        return MarketSnapshot.parse_trade_row(row)

    def load_trades(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int | None = None,
        chunk_size: int = 2_000_000,
    ) -> list[MarketSnapshot]:
        """Load trades as daily market snapshots, chunked to fit in RAM.

        Instead of loading all 86M+ rows, this:
        1. Reads the CSV in chunks
        2. Aggregates each chunk to daily per-market summaries (last price, total volume)
        3. Merges all chunk summaries
        4. Returns a manageable list of MarketSnapshots (~one per market per day)
        """
        csv_path = self._data_dir / "processed" / "trades.csv"
        if not csv_path.exists():
            raise FileNotFoundError(f"Trades CSV not found at {csv_path}")

        logger.info("Loading trades from %s (chunked)", csv_path)

        # Accumulate daily summaries across chunks
        # Key: (date_str, market_id) -> {price, volume, count}
        daily_agg: dict[tuple[str, str], dict] = {}

        progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(bar_width=40),
            TextColumn("[green]{task.completed:,} rows"),
            TimeElapsedColumn(),
        )
        with progress:
            task = progress.add_task("[cyan]Loading trades...", total=None)
            total_rows = 0

            reader = pl.read_csv_batched(
                str(csv_path),
                batch_size=chunk_size,
            )

            while True:
                batches = reader.next_batches(1)
                if not batches:
                    break

                chunk = batches[0]
                total_rows += len(chunk)
                progress.update(task, completed=total_rows)

                # Extract date from timestamp
                if "timestamp" in chunk.columns:
                    chunk = chunk.with_columns(
                        pl.col("timestamp").cast(pl.Utf8).str.slice(0, 10).alias("trade_date")
                    )
                else:
                    continue

                # Filter by date range
                if start_date:
                    chunk = chunk.filter(pl.col("trade_date") >= str(start_date))
                if end_date:
                    chunk = chunk.filter(pl.col("trade_date") <= str(end_date))

                if len(chunk) == 0:
                    continue

                # Aggregate: per (date, market_id) -> last price, total volume
                market_col = "market_id" if "market_id" in chunk.columns else "condition_id"
                if market_col not in chunk.columns:
                    continue

                price_col = "price" if "price" in chunk.columns else None
                vol_col = "usd_amount" if "usd_amount" in chunk.columns else "size" if "size" in chunk.columns else None

                if not price_col:
                    continue

                agg_exprs = [pl.col(price_col).last().alias("last_price")]
                if vol_col:
                    agg_exprs.append(pl.col(vol_col).sum().alias("total_volume"))

                daily = chunk.group_by(["trade_date", market_col]).agg(agg_exprs)

                for row in daily.iter_rows(named=True):
                    key = (row["trade_date"], row[market_col])
                    price = float(row.get("last_price", 0))
                    volume = float(row.get("total_volume", 0))

                    if key in daily_agg:
                        # Later chunk wins for price, volumes accumulate
                        daily_agg[key]["price"] = price
                        daily_agg[key]["volume"] += volume
                    else:
                        daily_agg[key] = {"price": price, "volume": volume}

            progress.update(task, description=f"[green]Read {total_rows:,} rows -> {len(daily_agg):,} daily snapshots")

        # Convert to MarketSnapshots
        snapshots = []
        for (date_str, market_id), agg in daily_agg.items():
            try:
                ts = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue

            snapshots.append(MarketSnapshot(
                polymarket_id=market_id or "",
                question="",
                category="unknown",
                token_id="",
                price=Decimal(str(round(agg["price"], 6))),
                volume=Decimal(str(round(agg["volume"], 2))),
                timestamp=ts,
            ))

        if limit:
            snapshots = snapshots[:limit]

        snapshots.sort(key=lambda s: s.timestamp)
        logger.info("Produced %d market snapshots from %d rows", len(snapshots), total_rows)
        return snapshots

    def load_resolutions(self) -> dict[str, dict]:
        """Load market resolution outcomes."""
        res_path = self._data_dir / "processed" / "resolutions.csv"
        if not res_path.exists():
            return {}

        df = pl.read_csv(str(res_path))
        resolutions = {}
        for row in df.iter_rows(named=True):
            resolutions[row.get("condition_id", "")] = {
                "outcome": row.get("outcome"),
                "final_price": float(row.get("final_price", 0)),
                "resolution_date": row.get("resolution_date"),
            }
        return resolutions

    @staticmethod
    def group_by_day(snapshots: list[MarketSnapshot]) -> dict[date, list[MarketSnapshot]]:
        """Group snapshots by calendar day."""
        by_day: dict[date, list[MarketSnapshot]] = defaultdict(list)
        for snap in snapshots:
            by_day[snap.timestamp.date()].append(snap)
        return dict(by_day)

    @staticmethod
    def aggregate_daily_markets(
        snapshots: list[MarketSnapshot],
    ) -> dict[str, MarketSnapshot]:
        """Get the last snapshot per market for a given day."""
        latest: dict[str, MarketSnapshot] = {}
        for snap in sorted(snapshots, key=lambda s: s.timestamp):
            latest[snap.polymarket_id] = snap
        return latest

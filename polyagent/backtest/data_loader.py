"""Load poly_data CSVs into time-ordered market snapshots."""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import polars as pl

from polyagent.models import MarketData

logger = logging.getLogger("polyagent.backtest.data_loader")


@dataclass
class MarketSnapshot:
    """A single market state at a point in time."""

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
        """Parse a single trade row from poly_data CSV."""
        ts_str = row.get("timestamp", "")
        if ts_str:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        else:
            ts = datetime.now(timezone.utc)

        return MarketSnapshot(
            polymarket_id=row.get("condition_id", ""),
            question=row.get("question", ""),
            category=row.get("category", "unknown"),
            token_id=row.get("token_id", ""),
            price=Decimal(str(row.get("price", 0))),
            volume=Decimal(str(row.get("size", 0))),
            timestamp=ts,
            outcome=row.get("outcome"),
        )


class DataLoader:
    """Loads and organizes historical market data for backtesting."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)

    @staticmethod
    def parse_trade_row(row: dict) -> MarketSnapshot:
        """Parse a single trade row from poly_data CSV."""
        return MarketSnapshot.parse_trade_row(row)

    def load_trades(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int | None = None,
    ) -> list[MarketSnapshot]:
        """Load trade data from poly_data CSVs using Polars."""
        csv_path = self._data_dir / "processed" / "trades.csv"
        if not csv_path.exists():
            raise FileNotFoundError(f"Trades CSV not found at {csv_path}")

        logger.info("Loading trades from %s", csv_path)
        df = pl.scan_csv(str(csv_path)).collect(streaming=True)

        if "timestamp" in df.columns:
            df = df.sort("timestamp")
            if start_date:
                df = df.filter(pl.col("timestamp") >= str(start_date))
            if end_date:
                df = df.filter(pl.col("timestamp") <= str(end_date))

        if limit:
            df = df.head(limit)

        snapshots = []
        for row in df.iter_rows(named=True):
            snapshots.append(MarketSnapshot.parse_trade_row(row))

        logger.info("Loaded %d trade snapshots", len(snapshots))
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
        """Aggregate multiple snapshots per market into a single daily state.

        Uses the last snapshot of the day per market (most recent price).
        """
        latest: dict[str, MarketSnapshot] = {}
        for snap in sorted(snapshots, key=lambda s: s.timestamp):
            latest[snap.polymarket_id] = snap
        return latest

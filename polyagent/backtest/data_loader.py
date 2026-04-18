"""Load trade data into hourly OHLCV bars for path-aware backtesting.

Source trade files are chunked in memory via Polars' batched reader; only the
aggregated hourly bars live in RAM.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import polars as pl
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from polyagent.models import MarketData

logger = logging.getLogger("polyagent.backtest.data_loader")


@dataclass
class HourlyBar:
    """One market's OHLCV bar over a single UTC hour."""

    market_id: str
    hour: datetime
    open: Decimal
    close: Decimal
    high: Decimal
    low: Decimal
    volume: Decimal
    first_ts: datetime
    last_ts: datetime
    question: str = ""
    category: str = "unknown"
    token_id: str = ""

    def to_market_data(self, hours_to_resolution: float, volume_24h: Decimal) -> MarketData:
        """Project this bar into the MarketData shape the scanner/executor expects."""
        return MarketData(
            polymarket_id=self.market_id,
            question=self.question,
            category=self.category,
            token_id=self.token_id,
            midpoint_price=self.close,
            bids_depth=self.volume,
            asks_depth=self.volume,
            hours_to_resolution=hours_to_resolution,
            volume_24h=volume_24h,
        )


class DataLoader:
    """Loads and organizes historical trade data as hourly bars."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)

    def load_hourly_bars(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        chunk_size: int = 2_000_000,
    ) -> list[HourlyBar]:
        """Load trades and aggregate into hourly OHLCV bars per market.

        Cross-chunk merge: for each (hour, market) key, keep the earliest
        observation's open and latest observation's close; accumulate high/low
        by max/min and volume by sum. This yields a stable aggregate no matter
        how chunk boundaries fall.
        """
        csv_path = self._data_dir / "processed" / "trades.csv"
        if not csv_path.exists():
            raise FileNotFoundError(f"Trades CSV not found at {csv_path}")

        logger.info("Loading trades from %s (chunked, hourly bars)", csv_path)

        bars: dict[tuple[str, str], dict] = {}

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

            reader = pl.read_csv_batched(str(csv_path), batch_size=chunk_size)

            while True:
                batches = reader.next_batches(1)
                if not batches:
                    break

                chunk = batches[0]
                total_rows += len(chunk)
                progress.update(task, completed=total_rows)

                if "timestamp" not in chunk.columns:
                    continue

                # Normalise timestamp to UTC datetime regardless of source format.
                # The ingest pipeline writes ISO strings after pl.from_epoch, but
                # raw Goldsky CSVs contain Unix-epoch integers — handle both.
                ts_dtype = chunk.schema["timestamp"]
                if ts_dtype in (pl.Int32, pl.Int64, pl.UInt32, pl.UInt64, pl.Float32, pl.Float64):
                    chunk = chunk.with_columns(
                        pl.from_epoch(pl.col("timestamp").cast(pl.Int64), time_unit="s")
                          .alias("_ts_dt")
                    )
                else:
                    chunk = chunk.with_columns(
                        pl.col("timestamp").cast(pl.Utf8)
                          .str.strptime(pl.Datetime("us", "UTC"), "%Y-%m-%d %H:%M:%S%.f", strict=False)
                          .alias("_ts_dt")
                    )

                chunk = chunk.with_columns(
                    pl.col("_ts_dt").dt.strftime("%Y-%m-%d").alias("trade_date"),
                    pl.col("_ts_dt").dt.strftime("%Y-%m-%dT%H").alias("hour_bucket"),
                    pl.col("_ts_dt").cast(pl.Utf8).alias("_ts_str"),
                )

                if start_date:
                    chunk = chunk.filter(pl.col("trade_date") >= str(start_date))
                if end_date:
                    chunk = chunk.filter(pl.col("trade_date") <= str(end_date))
                if len(chunk) == 0:
                    continue

                market_col = (
                    "market_id" if "market_id" in chunk.columns
                    else "condition_id" if "condition_id" in chunk.columns
                    else None
                )
                if not market_col:
                    continue
                if "price" not in chunk.columns:
                    continue

                vol_col = (
                    "usd_amount" if "usd_amount" in chunk.columns
                    else "size" if "size" in chunk.columns
                    else None
                )

                # Sort within chunk so first/last reflect wall-clock order.
                chunk = chunk.sort("_ts_str")

                agg_exprs = [
                    pl.col("price").first().alias("open"),
                    pl.col("price").last().alias("close"),
                    pl.col("price").max().alias("high"),
                    pl.col("price").min().alias("low"),
                    pl.col("_ts_str").first().alias("first_ts"),
                    pl.col("_ts_str").last().alias("last_ts"),
                ]
                if vol_col:
                    agg_exprs.append(pl.col(vol_col).sum().alias("volume"))

                hourly = chunk.group_by(["hour_bucket", market_col]).agg(agg_exprs)

                for row in hourly.iter_rows(named=True):
                    key = (row["hour_bucket"], row[market_col])
                    first_ts = str(row["first_ts"])
                    last_ts = str(row["last_ts"])
                    incoming = {
                        "open": float(row["open"]),
                        "close": float(row["close"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "volume": float(row.get("volume") or 0),
                        "first_ts": first_ts,
                        "last_ts": last_ts,
                    }

                    if key in bars:
                        existing = bars[key]
                        if first_ts < existing["first_ts"]:
                            existing["open"] = incoming["open"]
                            existing["first_ts"] = first_ts
                        if last_ts > existing["last_ts"]:
                            existing["close"] = incoming["close"]
                            existing["last_ts"] = last_ts
                        existing["high"] = max(existing["high"], incoming["high"])
                        existing["low"] = min(existing["low"], incoming["low"])
                        existing["volume"] += incoming["volume"]
                    else:
                        bars[key] = incoming

            progress.update(
                task,
                description=f"[green]Read {total_rows:,} rows -> {len(bars):,} hourly bars",
            )

        result: list[HourlyBar] = []
        for (hour_str, market_id), agg in bars.items():
            hour = _parse_hour_bucket(hour_str)
            if hour is None:
                continue
            result.append(HourlyBar(
                market_id=market_id or "",
                hour=hour,
                open=Decimal(str(round(agg["open"], 6))),
                close=Decimal(str(round(agg["close"], 6))),
                high=Decimal(str(round(agg["high"], 6))),
                low=Decimal(str(round(agg["low"], 6))),
                volume=Decimal(str(round(agg["volume"], 2))),
                first_ts=_parse_ts(agg["first_ts"]) or hour,
                last_ts=_parse_ts(agg["last_ts"]) or hour,
            ))

        result.sort(key=lambda b: (b.hour, b.market_id))
        logger.info("Built %d hourly bars across %d markets",
                    len(result), len({b.market_id for b in result}))
        return result

    def load_market_metadata(self) -> dict[str, dict]:
        """Load market questions and categories from markets.csv."""
        markets_path = self._data_dir / "markets.csv"
        if not markets_path.exists():
            logger.warning("No markets.csv found at %s", markets_path)
            return {}

        df = pl.read_csv(
            str(markets_path),
            schema_overrides={"token1": pl.Utf8, "token2": pl.Utf8},
        )
        metadata: dict[str, dict] = {}
        for row in df.iter_rows(named=True):
            question = row.get("question", "")
            category = self._detect_category(question)
            entry = {
                "question": question,
                "category": category,
                "token_id": row.get("token1", "") or row.get("token_id", ""),
            }
            market_id = row.get("id", "")
            condition_id = row.get("condition_id", "")
            if market_id:
                metadata[market_id] = entry
            if condition_id and condition_id != market_id:
                metadata[condition_id] = entry

        logger.info("Loaded metadata for %d markets (%d keys)", len(df), len(metadata))
        return metadata

    def load_resolutions(self) -> dict[str, dict]:
        """Load or derive resolution data per market."""
        res_path = self._data_dir / "processed" / "resolutions.csv"
        if res_path.exists():
            df = pl.read_csv(str(res_path))
            resolutions = {}
            for row in df.iter_rows(named=True):
                resolutions[row.get("condition_id", "")] = {
                    "outcome": row.get("outcome"),
                    "final_price": float(row.get("final_price", 0)),
                    "resolution_date": row.get("resolution_date"),
                }
            return resolutions

        logger.info("No resolutions.csv found, deriving from trades data...")
        return self._derive_resolutions()

    def _derive_resolutions(self, chunk_size: int = 2_000_000) -> dict[str, dict]:
        """Derive final price per market from the last observed trade."""
        csv_path = self._data_dir / "processed" / "trades.csv"
        if not csv_path.exists():
            return {}

        last_seen: dict[str, dict] = {}

        progress = Progress(
            SpinnerColumn(),
            TextColumn("[cyan]Deriving resolutions..."),
            BarColumn(bar_width=40),
            TextColumn("[green]{task.completed:,} rows"),
            TimeElapsedColumn(),
        )
        with progress:
            task = progress.add_task("resolutions", total=None)
            total_rows = 0

            reader = pl.read_csv_batched(str(csv_path), batch_size=chunk_size)
            while True:
                batches = reader.next_batches(1)
                if not batches:
                    break

                chunk = batches[0]
                total_rows += len(chunk)
                progress.update(task, completed=total_rows)

                market_col = (
                    "market_id" if "market_id" in chunk.columns
                    else "condition_id" if "condition_id" in chunk.columns
                    else None
                )
                if not market_col or "price" not in chunk.columns:
                    continue

                chunk = chunk.sort("timestamp")
                last_per_market = chunk.group_by(market_col).agg([
                    pl.col("price").last().alias("final_price"),
                    pl.col("timestamp").last().alias("last_ts"),
                ])

                for row in last_per_market.iter_rows(named=True):
                    mid = row[market_col]
                    if not mid:
                        continue
                    price = float(row["final_price"])
                    last_ts_str = str(row.get("last_ts", ""))
                    existing = last_seen.get(mid)
                    if existing is None or last_ts_str > str(existing.get("resolution_date", "")):
                        last_seen[mid] = {
                            "outcome": "Yes" if price > 0.5 else "No",
                            "final_price": price,
                            "resolution_date": last_ts_str,
                        }

        logger.info("Derived resolutions for %d markets", len(last_seen))
        return last_seen

    @staticmethod
    def _detect_category(question: str) -> str:
        q = (question or "").lower()
        if any(w in q for w in ["bitcoin", "btc", "eth", "crypto", "solana", "token"]):
            return "crypto"
        if any(w in q for w in ["trump", "biden", "election", "senate", "congress", "president", "governor"]):
            return "politics"
        if any(w in q for w in ["fed", "rate", "inflation", "gdp", "economy", "recession"]):
            return "macro"
        if any(w in q for w in ["nfl", "nba", "mlb", "soccer", "game", "match", "championship"]):
            return "sports"
        return "other"

    @staticmethod
    def group_by_hour(bars: list[HourlyBar]) -> dict[datetime, list[HourlyBar]]:
        """Chronological bucketing of bars for the engine's main loop."""
        by_hour: dict[datetime, list[HourlyBar]] = defaultdict(list)
        for b in bars:
            by_hour[b.hour].append(b)
        return dict(by_hour)


def _parse_hour_bucket(hour_str: str) -> datetime | None:
    """Parse a 'YYYY-MM-DDTHH' bucket string into a UTC datetime on the hour."""
    if not hour_str or len(hour_str) < 13:
        return None
    try:
        normalized = hour_str[:13].replace("T", " ")
        return datetime.strptime(normalized, "%Y-%m-%d %H").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_ts(ts: str | None) -> datetime | None:
    """Parse an ISO timestamp, tolerating missing timezone."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None

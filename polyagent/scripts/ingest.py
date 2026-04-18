"""Data ingestion pipeline — fetches and processes Polymarket historical data.

Three modes:
  polyagent ingest --snapshot    Download pre-built snapshot (~2GB), process into trades
  polyagent ingest --full        Scrape from Goldsky subgraph (slow, 2+ days first run)
  polyagent ingest --process     Just re-process existing orderFilled.csv into trades
  polyagent ingest --candles     Fetch OHLC price history for all markets via CLOB API
"""
from __future__ import annotations

import concurrent.futures
import csv
import json
import lzma
import logging
import os
import time
from calendar import timegm
from datetime import datetime, timezone
from pathlib import Path

import httpx
import polars as pl
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

logger = logging.getLogger("polyagent.scripts.ingest")

# Polymarket Gamma API for market metadata
GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"

# Goldsky subgraph for on-chain order events
GOLDSKY_URL = "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/orderbook-subgraph/0.0.1/gn"

# Pre-built snapshot (credits: @PendulumFlow)
SNAPSHOT_URL = "https://polydata-archive.s3.us-east-1.amazonaws.com/orderFilled_complete.csv.xz"

MARKET_HEADERS = [
    "createdAt", "id", "question", "answer1", "answer2", "neg_risk",
    "market_slug", "token1", "token2", "condition_id", "volume", "ticker", "closedTime",
]

ORDER_COLUMNS = [
    "timestamp", "maker", "makerAssetId", "makerAmountFilled",
    "taker", "takerAssetId", "takerAmountFilled", "transactionHash",
]


class DataIngester:
    """Fetches and processes Polymarket historical data."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "goldsky").mkdir(exist_ok=True)
        (self.data_dir / "processed").mkdir(exist_ok=True)
        self._http = httpx.Client(timeout=30.0)

    @property
    def markets_csv(self) -> Path:
        return self.data_dir / "markets.csv"

    @property
    def orders_csv(self) -> Path:
        return self.data_dir / "goldsky" / "orderFilled.csv"

    @property
    def trades_csv(self) -> Path:
        return self.data_dir / "processed" / "trades.csv"

    @property
    def candles_csv(self) -> Path:
        return self.data_dir / "processed" / "candles.csv"

    # ── Stage 1: Markets ────────────────────────────────────────────────

    def fetch_markets(self, batch_size: int = 500) -> int:
        """Fetch all market metadata from Polymarket Gamma API. Resumable."""
        offset = 0
        if self.markets_csv.exists():
            with open(self.markets_csv) as f:
                offset = sum(1 for _ in f) - 1  # subtract header
            logger.info("Resuming markets from offset %d", offset)

        mode = "a" if offset > 0 else "w"
        total_new = 0

        progress = Progress(
            SpinnerColumn(),
            TextColumn("[cyan]Fetching markets"),
            BarColumn(bar_width=40),
            TextColumn("[green]{task.completed:,} fetched"),
            TimeElapsedColumn(),
        )
        with progress, open(self.markets_csv, mode, newline="") as f:
            writer = csv.writer(f)
            if mode == "w":
                writer.writerow(MARKET_HEADERS)

            task = progress.add_task("markets", total=None)
            while True:
                try:
                    resp = self._http.get(
                        GAMMA_API_URL,
                        params={"order": "createdAt", "ascending": "true", "limit": batch_size, "offset": offset},
                    )
                    if resp.status_code == 429:
                        time.sleep(10)
                        continue
                    if resp.status_code >= 500:
                        time.sleep(5)
                        continue
                    resp.raise_for_status()
                except httpx.HTTPError as e:
                    logger.warning("Market fetch error: %s, retrying in 5s", e)
                    time.sleep(5)
                    continue

                markets = resp.json()
                if not markets:
                    break

                for m in markets:
                    row = self._parse_market_row(m)
                    if row:
                        writer.writerow(row)
                        total_new += 1

                offset += len(markets)
                progress.update(task, completed=offset)
                if len(markets) < batch_size:
                    break

        logger.info("Markets complete: %d new, %d total", total_new, offset)
        return offset

    def _parse_market_row(self, m: dict) -> list | None:
        """Parse a single market API response into a CSV row."""
        try:
            outcomes = json.loads(m.get("outcomes", "[]")) if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
            clob_tokens = json.loads(m.get("clobTokenIds", "[]")) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds", [])

            ticker = ""
            events = m.get("events", [])
            if events:
                ticker = events[0].get("ticker", "")

            return [
                m.get("createdAt", ""),
                m.get("id", ""),
                m.get("question", "") or m.get("title", ""),
                outcomes[0] if outcomes else "",
                outcomes[1] if len(outcomes) > 1 else "",
                m.get("negRiskAugmented", False) or m.get("negRiskOther", False),
                m.get("slug", ""),
                clob_tokens[0] if clob_tokens else "",
                clob_tokens[1] if len(clob_tokens) > 1 else "",
                m.get("conditionId", ""),
                m.get("volume", ""),
                ticker,
                m.get("closedTime", ""),
            ]
        except (json.JSONDecodeError, IndexError) as e:
            logger.warning("Failed to parse market %s: %s", m.get("id", "?"), e)
            return None

    # ── Stage 2a: Snapshot download ─────────────────────────────────────

    def download_snapshot(self) -> bool:
        """Download pre-built orderFilled snapshot. Pure Python, no system deps.

        Uses httpx for streaming download and stdlib lzma for decompression.
        Returns True on success.
        """
        target = self.orders_csv

        if target.exists():
            logger.info("orderFilled.csv already exists, skipping download")
            return True

        logger.info("Downloading snapshot from S3 (~2GB)...")
        try:
            with httpx.stream("GET", SNAPSHOT_URL, follow_redirects=True, timeout=600.0) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))

                progress = Progress(
                    SpinnerColumn(),
                    TextColumn("[cyan]Downloading snapshot"),
                    BarColumn(bar_width=40),
                    DownloadColumn(),
                    TransferSpeedColumn(),
                    TimeRemainingColumn(),
                )
                decompressor = lzma.LZMADecompressor()
                with progress, open(target, "wb") as f:
                    task = progress.add_task("download", total=total or None)
                    for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                        progress.advance(task, len(chunk))
                        try:
                            f.write(decompressor.decompress(chunk))
                        except lzma.LZMAError:
                            break

            logger.info("Snapshot ready: %s", target)
            return True
        except (httpx.HTTPError, lzma.LZMAError, OSError) as e:
            logger.error("Snapshot download failed: %s", e)
            # Clean up partial file
            if target.exists():
                target.unlink()
            return False

    # ── Stage 2b: Goldsky scrape (slow) ─────────────────────────────────

    def scrape_goldsky(self, batch_size: int = 1000) -> int:
        """Scrape order-filled events from Goldsky subgraph. Resumable."""
        last_timestamp = 0
        if self.orders_csv.exists():
            last_line = self._read_last_line(self.orders_csv)
            if last_line:
                try:
                    last_timestamp = int(last_line.split(",")[0]) - 1
                    logger.info("Resuming Goldsky from timestamp %d", last_timestamp)
                except (ValueError, IndexError):
                    pass

        total = 0
        query_template = """query {{
            orderFilledEvents(
                orderBy: timestamp, orderDirection: asc,
                first: {batch_size},
                where: {{timestamp_gt: "{ts}"}}
            ) {{
                timestamp maker makerAmountFilled makerAssetId
                taker takerAmountFilled takerAssetId transactionHash
            }}
        }}"""

        progress = Progress(
            SpinnerColumn(),
            TextColumn("[cyan]Scraping Goldsky"),
            BarColumn(bar_width=40),
            TextColumn("[green]{task.completed:,} events"),
            TimeElapsedColumn(),
        )
        with progress:
            task = progress.add_task("goldsky", total=None)
            while True:
                query = query_template.format(batch_size=batch_size, ts=last_timestamp)
                try:
                    resp = self._http.post(GOLDSKY_URL, json={"query": query})
                    resp.raise_for_status()
                    events = resp.json().get("data", {}).get("orderFilledEvents", [])
                except (httpx.HTTPError, json.JSONDecodeError) as e:
                    logger.warning("Goldsky error: %s, retrying in 5s", e)
                    time.sleep(5)
                    continue

                if not events:
                    break

                # Append to CSV
                file_exists = self.orders_csv.exists()
                with open(self.orders_csv, "a", newline="") as f:
                    writer = csv.writer(f)
                    if not file_exists:
                        writer.writerow(ORDER_COLUMNS)
                    for e in events:
                        writer.writerow([e.get(c, "") for c in ORDER_COLUMNS])

                last_timestamp = int(events[-1]["timestamp"])
                total += len(events)
                progress.update(task, completed=total)

                if len(events) < batch_size:
                    break

        logger.info("Goldsky scrape complete: %d new events", total)
        return total

    # ── Stage 3: Process into trades ────────────────────────────────────

    def process_trades(self, chunk_size: int = 5_000_000) -> int:
        """Process orderFilled events + markets into structured trades.

        Processes in chunks to handle datasets larger than available RAM.
        A 37GB CSV with 86M+ rows needs chunked processing even with 48GB RAM.
        """
        if not self.orders_csv.exists():
            raise FileNotFoundError(f"No order data at {self.orders_csv}")
        if not self.markets_csv.exists():
            raise FileNotFoundError(f"No market data at {self.markets_csv}")

        progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(bar_width=40),
            TextColumn("[green]{task.completed:,} rows"),
            TimeElapsedColumn(),
        )
        with progress:
            # Markets are small (~20MB), load fully
            task_m = progress.add_task("[cyan]Loading markets...", total=None)
            markets = pl.read_csv(
                str(self.markets_csv),
                schema_overrides={"token1": pl.Utf8, "token2": pl.Utf8},
            )
            progress.update(task_m, description=f"[green]Loaded {len(markets):,} markets")
            progress.update(task_m, completed=1, total=1)

            # Build token -> market_id lookup (small, stays in memory)
            markets_long = (
                markets.select(["id", "token1", "token2"])
                .rename({"id": "market_id"})
                .melt(id_vars="market_id", value_vars=["token1", "token2"],
                      variable_name="side", value_name="asset_id")
            )

            # Count total lines for progress (fast — just counts newlines)
            task_count = progress.add_task("[cyan]Counting rows...", total=None)
            total_rows = 0
            with open(self.orders_csv, "rb") as f:
                for buf in iter(lambda: f.read(1024 * 1024 * 64), b""):
                    total_rows += buf.count(b"\n")
            total_rows -= 1  # subtract header
            progress.update(task_count, description=f"[green]{total_rows:,} rows to process")
            progress.update(task_count, completed=1, total=1)

            # Process in chunks using batched reader
            task_proc = progress.add_task("[cyan]Processing trades...", total=total_rows)
            reader = pl.read_csv_batched(
                str(self.orders_csv),
                batch_size=chunk_size,
                schema_overrides={"takerAssetId": pl.Utf8, "makerAssetId": pl.Utf8},
            )

            total_trades = 0
            first_write = True

            while True:
                batches = reader.next_batches(1)
                if not batches:
                    break

                chunk = batches[0]

                # Transform this chunk
                chunk = chunk.with_columns(
                    pl.from_epoch(pl.col("timestamp"), time_unit="s").alias("timestamp")
                )

                chunk = chunk.with_columns(
                    pl.when(pl.col("makerAssetId") != "0")
                    .then(pl.col("makerAssetId"))
                    .otherwise(pl.col("takerAssetId"))
                    .alias("nonusdc_asset_id")
                )

                chunk = chunk.join(markets_long, left_on="nonusdc_asset_id", right_on="asset_id", how="left")

                chunk = chunk.with_columns([
                    pl.when(pl.col("makerAssetId") == "0").then(pl.lit("USDC")).otherwise(pl.col("side")).alias("makerAsset"),
                    pl.when(pl.col("takerAssetId") == "0").then(pl.lit("USDC")).otherwise(pl.col("side")).alias("takerAsset"),
                ])

                chunk = chunk.with_columns([
                    (pl.col("makerAmountFilled") / 10**6).alias("makerAmountFilled"),
                    (pl.col("takerAmountFilled") / 10**6).alias("takerAmountFilled"),
                ])

                chunk = chunk.with_columns([
                    pl.when(pl.col("takerAsset") == "USDC").then(pl.lit("BUY")).otherwise(pl.lit("SELL")).alias("taker_direction"),
                    pl.when(pl.col("takerAsset") == "USDC").then(pl.lit("SELL")).otherwise(pl.lit("BUY")).alias("maker_direction"),
                    pl.when(pl.col("takerAsset") == "USDC")
                    .then(pl.col("takerAmountFilled") / pl.col("makerAmountFilled"))
                    .otherwise(pl.col("makerAmountFilled") / pl.col("takerAmountFilled"))
                    .cast(pl.Float64)
                    .alias("price"),
                    pl.when(pl.col("takerAsset") == "USDC")
                    .then(pl.col("takerAmountFilled"))
                    .otherwise(pl.col("makerAmountFilled"))
                    .alias("usd_amount"),
                    pl.when(pl.col("takerAsset") != "USDC")
                    .then(pl.col("takerAmountFilled"))
                    .otherwise(pl.col("makerAmountFilled"))
                    .alias("token_amount"),
                    pl.when(pl.col("makerAsset") != "USDC")
                    .then(pl.col("makerAsset"))
                    .otherwise(pl.col("takerAsset"))
                    .alias("nonusdc_side"),
                ])

                trades = chunk.select([
                    "timestamp", "market_id", "maker", "taker", "nonusdc_side",
                    "maker_direction", "taker_direction", "price", "usd_amount",
                    "token_amount", "transactionHash",
                ]).filter(pl.col("market_id").is_not_null())

                # Append to output file
                if first_write:
                    trades.write_csv(str(self.trades_csv))
                    first_write = False
                else:
                    with open(self.trades_csv, "a") as f:
                        trades.write_csv(f, include_header=False)

                total_trades += len(trades)
                progress.update(task_proc, completed=total_trades)

            progress.update(task_proc, description=f"[green]Processed {total_trades:,} trades")

        return total_trades

    # ── Stage 4: CLOB candles ────────────────────────────────────────────

    def fetch_candles(
        self,
        since: str | None = None,
        fidelity: int = 60,
        workers: int = 8,
    ) -> int:
        """Fetch OHLC price history from the CLOB API for all markets in markets.csv.

        For each token in markets.csv, calls GET
        https://clob.polymarket.com/prices-history?market={token_id}&fidelity={fidelity}
        and aggregates the tick-level (t, p) pairs into hourly OHLCV bars.

        Args:
            since: Optional ISO date string (YYYY-MM-DD). Only fetch candles
                   after this date if provided.
            fidelity: Candle fidelity in minutes (default 60 = 1h buckets).
            workers: Thread pool size for parallel HTTP calls.

        Returns:
            Total number of hourly bars written to candles.csv.
        """
        if not self.markets_csv.exists():
            raise FileNotFoundError(f"No markets.csv found at {self.markets_csv}")

        # Determine start timestamp if --since was given.
        start_ts: int | None = None
        if since:
            try:
                dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                start_ts = int(dt.timestamp())
            except ValueError:
                logger.warning("Could not parse --since value %r; ignoring", since)

        # Load market token list.
        markets = []
        with open(self.markets_csv, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                for col in ("token1", "token2"):
                    token_id = (row.get(col) or "").strip()
                    if token_id:
                        markets.append((
                            token_id,
                            row.get("id", ""),
                            row.get("question", ""),
                            row.get("condition_id", ""),
                        ))

        # Determine which tokens are already in candles.csv (skip mode).
        already_done: set[str] = set()
        if self.candles_csv.exists():
            try:
                with open(self.candles_csv, newline="") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        tid = (row.get("token_id") or "").strip()
                        if tid:
                            already_done.add(tid)
                logger.info("Skipping %d already-fetched tokens", len(already_done))
            except Exception as e:
                logger.warning("Could not read existing candles.csv for dedup: %s", e)

        tokens_to_fetch = [m for m in markets if m[0] not in already_done]
        logger.info(
            "Fetching candles for %d tokens (%d already done)",
            len(tokens_to_fetch),
            len(already_done),
        )

        # Shared HTTP client — httpx.Client is thread-safe for reads.
        http = httpx.Client(timeout=30.0)

        CLOB_URL = "https://clob.polymarket.com/prices-history"

        def fetch_token(args: tuple[str, str, str, str]) -> list[dict]:
            """Fetch + aggregate candles for one token. Returns list of bar dicts."""
            token_id, market_id, question, condition_id = args
            time.sleep(0.05)
            try:
                params: dict = {"market": token_id, "fidelity": fidelity}
                if start_ts is not None:
                    params["startTs"] = start_ts
                resp = http.get(CLOB_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
                history = data.get("history", [])
            except (httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
                logger.warning("Skipping token %s: %s", token_id, e)
                return []

            if not history:
                return []

            # Aggregate into hourly buckets.
            buckets: dict[int, list[float]] = {}
            for tick in history:
                try:
                    t = int(tick["t"])
                    p = float(tick["p"])
                except (KeyError, TypeError, ValueError):
                    continue
                hour_ts = t // 3600 * 3600
                buckets.setdefault(hour_ts, []).append(p)

            bars = []
            for hour_ts, prices in sorted(buckets.items()):
                dt = datetime.fromtimestamp(hour_ts, tz=timezone.utc)
                bars.append({
                    "timestamp": dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "market_id": market_id,
                    "condition_id": condition_id,
                    "token_id": token_id,
                    "open": prices[0],
                    "high": max(prices),
                    "low": min(prices),
                    "close": prices[-1],
                    "volume": len(prices),
                })
            return bars

        # Ensure the output file has a header if it doesn't exist yet.
        candles_columns = [
            "timestamp", "market_id", "condition_id", "token_id",
            "open", "high", "low", "close", "volume",
        ]
        file_exists = self.candles_csv.exists()

        total_bars = 0
        processed = 0

        progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(bar_width=40),
            TextColumn("[green]{task.completed:,}/{task.total:,} tokens"),
            TimeElapsedColumn(),
        )
        with progress, open(self.candles_csv, "a", newline="") as out_f:
            writer = csv.DictWriter(out_f, fieldnames=candles_columns)
            if not file_exists:
                writer.writeheader()

            task = progress.add_task(
                "[cyan]Fetching candles...",
                total=len(tokens_to_fetch),
            )

            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(fetch_token, args): args for args in tokens_to_fetch}
                for future in concurrent.futures.as_completed(futures):
                    bars = future.result()
                    if bars:
                        writer.writerows(bars)
                        out_f.flush()
                        total_bars += len(bars)
                    processed += 1
                    progress.update(
                        task,
                        completed=processed,
                        description=f"[cyan]Fetching candles... {total_bars:,} bars written",
                    )

        http.close()
        logger.info(
            "Candles complete: %d bars written for %d tokens -> %s",
            total_bars,
            len(tokens_to_fetch),
            self.candles_csv,
        )
        return total_bars

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _read_last_line(path: Path) -> str:
        """Read the last line of a file without loading it all into memory."""
        with open(path, "rb") as f:
            f.seek(0, 2)  # seek to end
            pos = f.tell()
            if pos == 0:
                return ""
            # Walk backwards to find the last newline
            while pos > 0:
                pos -= 1
                f.seek(pos)
                if f.read(1) == b"\n" and pos < f.seek(0, 2) - 1:
                    break
            f.seek(pos + 1 if pos > 0 else 0)
            return f.readline().decode().strip()

    def close(self) -> None:
        """Close the HTTP client."""
        self._http.close()

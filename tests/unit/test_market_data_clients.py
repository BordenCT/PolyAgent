"""Unit tests for the Coinbase + Bybit WS clients.

Tests drive each client by feeding decoded message dicts directly
through ``process_message`` rather than standing up the WS handshake
and the reconnect loop. That keeps tests deterministic and fast and
avoids any chance of a hung test on an unfinished async iterator.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from polyagent.data.clients.bybit import BybitWSClient, MarkIndexUpdate
from polyagent.data.clients.coinbase_ws import (
    CoinbaseWSClient,
    OrderbookSnapshot,
    TradePrint,
)


@pytest.mark.asyncio
async def test_coinbase_l2_snapshot_populates_book():
    captured: list[OrderbookSnapshot] = []

    async def on_snap(s: OrderbookSnapshot) -> None:
        captured.append(s)

    client = CoinbaseWSClient(product_id="BTC-USD", on_snapshot=on_snap)
    await client.process_message({
        "channel": "l2_data",
        "events": [{
            "type": "snapshot",
            "product_id": "BTC-USD",
            "updates": [
                {"side": "bid",   "price_level": "100.0", "new_quantity": "1.0"},
                {"side": "bid",   "price_level": "99.0",  "new_quantity": "2.0"},
                {"side": "offer", "price_level": "101.0", "new_quantity": "0.5"},
            ],
        }],
    })
    assert len(captured) == 1
    snap = captured[0]
    assert snap.best_bid == Decimal("100.0")
    assert snap.best_ask == Decimal("101.0")
    assert snap.mid == Decimal("100.5")
    # bid volume = 3, ask volume = 0.5 -> imbalance > 0
    assert snap.imbalance is not None and snap.imbalance > 0


@pytest.mark.asyncio
async def test_coinbase_l2_update_removes_level_on_zero_quantity():
    client = CoinbaseWSClient(product_id="BTC-USD")
    await client.process_message({
        "channel": "l2_data",
        "events": [{
            "type": "snapshot",
            "updates": [
                {"side": "bid", "price_level": "100.0", "new_quantity": "1.0"},
                {"side": "bid", "price_level": "99.0",  "new_quantity": "2.0"},
            ],
        }],
    })
    await client.process_message({
        "channel": "l2_data",
        "events": [{
            "type": "update",
            "updates": [
                {"side": "bid", "price_level": "100.0", "new_quantity": "0"},
            ],
        }],
    })
    snap = client.snapshot_topN(5)
    # Top bid should fall through to the next level; the deleted level
    # is gone from the book.
    assert snap.best_bid == Decimal("99.0")


@pytest.mark.asyncio
async def test_coinbase_market_trades_emit_normalized_prints():
    captured: list[TradePrint] = []

    async def on_trade(tr: TradePrint) -> None:
        captured.append(tr)

    client = CoinbaseWSClient(product_id="BTC-USD", on_trade=on_trade)
    await client.process_message({
        "channel": "market_trades",
        "events": [{
            "type": "snapshot",
            "trades": [
                {"trade_id": "t1", "product_id": "BTC-USD",
                 "price": "100.5", "size": "0.25", "side": "BUY",
                 "time": "2026-05-07T10:00:00.000000Z"},
                {"trade_id": "t2", "product_id": "BTC-USD",
                 "price": "100.4", "size": "0.10", "side": "SELL",
                 "time": "2026-05-07T10:00:01.000000Z"},
            ],
        }],
    })
    assert len(captured) == 2
    assert captured[0].side == "buy"  # lower-cased
    assert captured[0].price == Decimal("100.5")
    assert captured[1].side == "sell"


@pytest.mark.asyncio
async def test_bybit_orderbook_snapshot_and_delta():
    client = BybitWSClient(symbol="BTCUSDT")
    await client.process_message({
        "topic": "orderbook.50.BTCUSDT",
        "type": "snapshot",
        "data": {
            "s": "BTCUSDT",
            "b": [["100.0", "1.0"], ["99.0", "2.0"]],
            "a": [["101.0", "0.5"], ["102.0", "1.0"]],
        },
    })
    await client.process_message({
        "topic": "orderbook.50.BTCUSDT",
        "type": "delta",
        "data": {
            "s": "BTCUSDT",
            "b": [["100.0", "0"]],
            "a": [["100.5", "0.3"]],
        },
    })
    s = client.snapshot_topN(5)
    assert s.best_bid == Decimal("99.0")
    assert s.best_ask == Decimal("100.5")


@pytest.mark.asyncio
async def test_bybit_public_trade_emits_normalized_prints():
    captured: list[TradePrint] = []

    async def on_trade(tr: TradePrint) -> None:
        captured.append(tr)

    client = BybitWSClient(symbol="BTCUSDT", on_trade=on_trade)
    await client.process_message({
        "topic": "publicTrade.BTCUSDT",
        "data": [
            {"i": "tx1", "T": 1715000000000, "s": "BTCUSDT",
             "S": "Buy",  "p": "100.5", "v": "0.25"},
            {"i": "tx2", "T": 1715000001000, "s": "BTCUSDT",
             "S": "Sell", "p": "100.4", "v": "0.10"},
        ],
    })
    assert [t.side for t in captured] == ["buy", "sell"]


@pytest.mark.asyncio
async def test_bybit_tickers_emits_mark_index_with_basis():
    captured: list[MarkIndexUpdate] = []

    async def on_mark(m: MarkIndexUpdate) -> None:
        captured.append(m)

    client = BybitWSClient(symbol="BTCUSDT", on_mark=on_mark)
    await client.process_message({
        "topic": "tickers.BTCUSDT",
        "ts": 1715000000000,
        "data": {
            "symbol": "BTCUSDT",
            "markPrice": "65000.5",
            "indexPrice": "65000.0",
            "lastPrice": "65001.0",
        },
    })
    assert len(captured) == 1
    m = captured[0]
    assert m.mark_price == Decimal("65000.5")
    assert m.basis == Decimal("0.5")


@pytest.mark.asyncio
async def test_bybit_tickers_basis_handles_partial_data():
    """Bybit sends partial ticker updates with only the fields that changed.
    Without index price the basis is None rather than 0 or a crash."""
    captured: list[MarkIndexUpdate] = []

    async def on_mark(m: MarkIndexUpdate) -> None:
        captured.append(m)

    client = BybitWSClient(symbol="BTCUSDT", on_mark=on_mark)
    await client.process_message({
        "topic": "tickers.BTCUSDT",
        "ts": 1715000000000,
        "data": {"symbol": "BTCUSDT", "markPrice": "65000.5"},
    })
    assert captured[0].basis is None
    assert captured[0].mark_price == Decimal("65000.5")

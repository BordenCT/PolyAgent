"""Unit tests for the Coinbase + Bybit WS clients.

Tests drive each client by feeding decoded message dicts directly
through ``process_message`` rather than standing up the WS handshake
and the reconnect loop. That keeps tests deterministic and fast and
avoids any chance of a hung test on an unfinished async iterator.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from polyagent.data.clients.bybit import BybitWSClient, FundingPoint, MarkIndexUpdate
from polyagent.data.clients.coinbase_exchange_ws import CoinbaseExchangeWSClient
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
async def test_bybit_tickers_merges_partial_deltas_into_full_state():
    """Bybit emits ticker DELTAS (only the changed field). The client must
    merge them into a running state so each downstream emit carries the
    last-known mark, index, last_price, and a basis whenever both legs
    have ever been seen."""
    captured: list[MarkIndexUpdate] = []

    async def on_mark(m: MarkIndexUpdate) -> None:
        captured.append(m)

    client = BybitWSClient(symbol="BTCUSDT", on_mark=on_mark)
    # First delta: only mark; basis still None.
    await client.process_message({
        "topic": "tickers.BTCUSDT", "ts": 1,
        "data": {"symbol": "BTCUSDT", "markPrice": "65000.5"},
    })
    # Second delta: only index; running state now has both legs and
    # basis becomes computable.
    await client.process_message({
        "topic": "tickers.BTCUSDT", "ts": 2,
        "data": {"symbol": "BTCUSDT", "indexPrice": "65000.0"},
    })
    # Third delta: only lastPrice; mark + index carry over from prior state.
    await client.process_message({
        "topic": "tickers.BTCUSDT", "ts": 3,
        "data": {"symbol": "BTCUSDT", "lastPrice": "65001.0"},
    })
    assert len(captured) == 3
    assert captured[0].basis is None
    assert captured[1].basis == Decimal("0.5")
    assert captured[2].mark_price == Decimal("65000.5")
    assert captured[2].index_price == Decimal("65000.0")
    assert captured[2].last_price == Decimal("65001.0")
    assert captured[2].basis == Decimal("0.5")


@pytest.mark.asyncio
async def test_bybit_tickers_emit_funding_only_when_rate_changes():
    """Bybit's REST funding endpoint geo-blocks US IPs (403), so we read
    fundingRate off the WS tickers stream instead. Two requirements:
    (a) the first tick with a fundingRate emits a FundingPoint, and
    (b) subsequent ticks with the same rate do not re-emit."""
    captured: list[FundingPoint] = []

    async def on_funding(p: FundingPoint) -> None:
        captured.append(p)

    client = BybitWSClient(symbol="BTCUSDT", on_funding=on_funding)
    base = {"topic": "tickers.BTCUSDT", "ts": 1, "data": {"symbol": "BTCUSDT"}}
    # First emit: a rate appears -> persist.
    await client.process_message({**base, "data": {**base["data"], "fundingRate": "0.0001"}})
    # Same rate restated by a later delta -> should NOT re-emit.
    await client.process_message({**base, "data": {**base["data"], "fundingRate": "0.0001"}})
    # Rate changes (next funding cycle) -> emit again.
    await client.process_message({**base, "data": {**base["data"], "fundingRate": "0.00012"}})
    assert len(captured) == 2
    assert captured[0].funding_rate == Decimal("0.0001")
    assert captured[0].annualised_rate == Decimal("0.0001") * Decimal("1095")
    assert captured[1].funding_rate == Decimal("0.00012")


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


# ---- Coinbase Exchange (Pro) WS client ----

@pytest.mark.asyncio
async def test_coinbase_exchange_snapshot_then_l2update():
    client = CoinbaseExchangeWSClient(product_id="BTC-USD")
    await client.process_message({
        "type": "snapshot",
        "product_id": "BTC-USD",
        "bids": [["100.0", "1.0"], ["99.0", "2.0"]],
        "asks": [["101.0", "0.5"], ["102.0", "1.0"]],
    })
    await client.process_message({
        "type": "l2update",
        "product_id": "BTC-USD",
        "changes": [
            ["buy",  "100.0", "0"],     # remove top bid
            ["sell", "100.5", "0.3"],   # insert ask inside spread
        ],
    })
    s = client.snapshot_topN(5)
    assert s.best_bid == Decimal("99.0")
    assert s.best_ask == Decimal("100.5")


@pytest.mark.asyncio
async def test_coinbase_exchange_match_inverts_maker_to_aggressor():
    """Coinbase Exchange ``matches`` reports the maker side. Convention
    everywhere else in the system is the aggressor side, so the client
    must invert: maker buy -> aggressor sell, maker sell -> aggressor buy."""
    captured: list[TradePrint] = []

    async def on_trade(tr: TradePrint) -> None:
        captured.append(tr)

    client = CoinbaseExchangeWSClient(product_id="BTC-USD", on_trade=on_trade)
    await client.process_message({
        "type": "match", "trade_id": 12345,
        "product_id": "BTC-USD", "side": "sell",
        "price": "100.5", "size": "0.25",
        "time": "2026-05-08T01:00:00.000000Z",
    })
    await client.process_message({
        "type": "match", "trade_id": 12346,
        "product_id": "BTC-USD", "side": "buy",
        "price": "100.4", "size": "0.10",
        "time": "2026-05-08T01:00:01.000000Z",
    })
    assert len(captured) == 2
    # maker sell -> aggressor buy
    assert captured[0].side == "buy"
    assert captured[0].price == Decimal("100.5")
    # maker buy -> aggressor sell
    assert captured[1].side == "sell"


@pytest.mark.asyncio
async def test_coinbase_exchange_ignores_unknown_message_types():
    """Subscriptions, heartbeats, and any other server messages must not
    crash or affect the local book / callback counts."""
    captured: list[TradePrint] = []

    async def on_trade(tr: TradePrint) -> None:
        captured.append(tr)

    client = CoinbaseExchangeWSClient(product_id="BTC-USD", on_trade=on_trade)
    for msg in [
        {"type": "subscriptions", "channels": []},
        {"type": "heartbeat", "sequence": 1},
        {"type": "error", "message": "test"},
        {"type": "ticker"},  # not subscribed; should be ignored
    ]:
        await client.process_message(msg)
    assert captured == []
    snap = client.snapshot_topN(5)
    assert snap.bids == [] and snap.asks == []


@pytest.mark.asyncio
async def test_coinbase_exchange_venue_label_matches_advanced_trade():
    """Both Coinbase clients report venue='coinbase' so existing analytics
    queries don't need to know which feed produced the row."""
    client = CoinbaseExchangeWSClient(product_id="BTC-USD")
    assert client.venue == "coinbase"

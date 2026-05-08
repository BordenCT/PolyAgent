"""Diagnostic probe for the Coinbase Exchange WS ``matches`` channel.

Why this exists: ingestion is collecting Coinbase orderbook snapshots
fine but zero trade prints. This script connects to the same endpoint
the bot uses, subscribes to ``matches`` only (so the subscription
response unambiguously shows whether matches was accepted), and prints
the next few raw messages.

Run on the algotrader box (or any box with outbound network access to
ws-feed.exchange.coinbase.com):

    uv run python scripts/probe_coinbase_matches.py

Expected output, in order:
    1. ``connected``
    2. ``subscribed``
    3. msg 0: a ``subscriptions`` confirmation. The ``channels`` field
       must list ``matches``; if not, our subscribe was rejected.
    4. msg 1: typically a ``last_match`` (one-time backfill of the most
       recent trade).
    5. msg 2-7: ``match`` events as new trades arrive.

If we get the subscriptions confirmation but no match events for ~30
seconds, BTC-USD is unusually quiet (rare). If matches isn't in the
subscriptions confirmation at all, Coinbase is silently dropping it
from the multi-channel subscribe and we need to subscribe matches in
its own request.
"""
from __future__ import annotations

import asyncio
import json
import sys

import websockets


_WS_URL = "wss://ws-feed.exchange.coinbase.com"
_PRODUCT = "BTC-USD"
_MAX_MESSAGES = 8
_OVERALL_TIMEOUT_S = 20.0


async def _probe() -> None:
    async with websockets.connect(_WS_URL, max_size=10 * 1024 * 1024) as ws:
        print("connected", flush=True)
        await ws.send(json.dumps({
            "type": "subscribe",
            "product_ids": [_PRODUCT],
            "channels": ["matches"],
        }))
        print("subscribed", flush=True)
        for i in range(_MAX_MESSAGES):
            msg = await ws.recv()
            print(f"--- msg {i} ---", flush=True)
            print(msg, flush=True)


async def main() -> int:
    try:
        await asyncio.wait_for(_probe(), timeout=_OVERALL_TIMEOUT_S)
        return 0
    except asyncio.TimeoutError:
        print(f"TIMEOUT after {_OVERALL_TIMEOUT_S}s with fewer than "
              f"{_MAX_MESSAGES} messages received", file=sys.stderr, flush=True)
        return 2
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Single-thread quant orchestrator.

Replaces the previous ``run_btc5m_worker`` loop. Drives one timer for each
asset's tick cadence plus a single shared market-poll cadence. Faults in
any one ``PriceSource.tick`` or in ``scan_and_decide`` are caught and
logged so a flaky upstream cannot stall the rest of the loop.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from polyagent.services.quant.assets.sources.base import PriceSource

logger = logging.getLogger("polyagent.services.quant.orchestrator")


def _safely(fn: Callable, *args, **kwargs) -> None:
    """Run ``fn(*args, **kwargs)`` and swallow any exception with a log."""
    try:
        fn(*args, **kwargs)
    except Exception:
        logger.exception("orchestrator: %s raised", getattr(fn, "__name__", repr(fn)))


def run_quant_orchestrator(
    sources: dict[str, PriceSource],
    specs: list,
    scan_and_decide: Callable[[], None],
    market_interval_s: float,
    shutdown_q,
) -> None:
    """Drive ticks and market polls until shutdown_q is non-empty.

    Args:
        sources: Mapping ``asset_id -> PriceSource``. Each tick is invoked
            on its asset's own cadence and isolated by ``_safely``.
        specs: Iterable of objects exposing ``asset_id`` and
            ``tick_interval_s`` (typically :class:`AssetSpec`).
        scan_and_decide: Callable that polls Gamma for new markets and runs
            decisions. Invoked at most once per ``market_interval_s``.
        market_interval_s: Seconds between calls to ``scan_and_decide``.
        shutdown_q: Any object with a non-blocking ``empty()`` method
            (typically :class:`queue.Queue`). Loop exits cleanly when it
            becomes non-empty.
    """
    by_id = {s.asset_id: s for s in specs}
    last_tick_at = {asset_id: 0.0 for asset_id in sources}
    last_market_poll = 0.0
    min_tick_interval = min((s.tick_interval_s for s in specs), default=1.0)

    logger.info(
        "quant orchestrator started: assets=%s, market_poll=%ss, min_tick=%ss",
        sorted(sources.keys()), market_interval_s, min_tick_interval,
    )

    try:
        while shutdown_q.empty():
            now = time.time()
            for asset_id, src in sources.items():
                spec = by_id.get(asset_id)
                if spec is None:
                    continue
                if now - last_tick_at[asset_id] >= spec.tick_interval_s:
                    _safely(src.tick)
                    last_tick_at[asset_id] = now

            if now - last_market_poll >= market_interval_s:
                _safely(scan_and_decide)
                last_market_poll = now

            time.sleep(min_tick_interval)
    finally:
        for src in sources.values():
            _safely(src.close)
        logger.info("quant orchestrator stopped")

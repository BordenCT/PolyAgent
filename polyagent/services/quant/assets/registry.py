"""Asset registry: declares supported assets, read API, env overrides.

To add a new asset:
1. Append an entry to ASSETS below.
2. If the asset needs a new price/settlement source, add it under
   polyagent/services/quant/assets/sources/.
3. Update tests in tests/unit/test_quant_assets_registry.py.
"""
from __future__ import annotations

import os
from dataclasses import replace

from polyagent.services.quant.assets.sources.chainlink import ChainlinkDataFeedSource
from polyagent.services.quant.assets.sources.coinbase import CoinbaseSpotSource
from polyagent.services.quant.assets.spec import (
    AssetClass, AssetSpec, MarketFamily,
)
from polyagent.services.quant.core.vol import VolCalibration, VolMethod


def _btc_source():
    """BTC price+settlement source.

    Picks at construction time:
      - Chainlink Polygon BTC/USD when POLYGON_RPC_URL is set in env,
      - Coinbase BTC-USD otherwise.

    Chainlink aligns with Polymarket's settlement oracle (see
    docs/feat/btc-5m-roadmap.md Phase 3) and removes the basis drift
    that inflates paper-P&L relative to actual PM outcomes. But
    Chainlink is on-chain and free public Polygon RPCs throttle our
    poll loop; without a private endpoint the source returns None on
    most calls, the decider's start_spot lazy-fetch falls back to
    current_spot (S == K), p_up collapses to ~0.5, edge collapses
    below threshold, and no markets are entered.

    Coinbase has generous rate limits and ticks reliably at the
    sub-second cadence the orchestrator uses, so the bot keeps
    trading. The resolver uses Polymarket's actual settlement
    (PolymarketClient.fetch_market_state) regardless of which source
    is configured, so accounting stays honest either way; only the
    decider's entry-time edge calculation is affected.

    Set POLYGON_RPC_URL=<private endpoint URL> to flip to Chainlink.
    """
    rpc_url = os.environ.get("POLYGON_RPC_URL")
    if rpc_url:
        return ChainlinkDataFeedSource(pair="BTC-USD", rpc_url=rpc_url)
    return CoinbaseSpotSource("BTC-USD")


ASSETS: dict[str, AssetSpec] = {
    "BTC": AssetSpec(
        asset_id="BTC",
        asset_class=AssetClass.CRYPTO,
        # Source choice is env-conditional inside _btc_source():
        # Chainlink (settlement-aligned but rate-limited) when
        # POLYGON_RPC_URL is set, Coinbase (fast, free) otherwise.
        # Resolver always uses Polymarket truth for the outcome, so
        # accounting is correct either way; only the entry-time edge
        # calculation is affected by the source choice.
        price_source=_btc_source,
        settlement_source=_btc_source,
        default_vol=0.60,
        vol_calibration=VolCalibration(
            method=VolMethod.HYBRID,
            rolling_min_s=300,
            rolling_max_s=24 * 3600,
            rolling_horizon_multiplier=4.0,
            fixed_value=0.60,
            hybrid_threshold_s=4 * 3600,
        ),
        supported_market_families=frozenset({
            MarketFamily.SHORT_HORIZON, MarketFamily.STRIKE, MarketFamily.RANGE,
        }),
        # paper_only stays True until we accumulate ~24h of paper trades on
        # the Chainlink-aligned source and confirm the |edge| -> outcome
        # signal is calibrated. Flip via env when ready: QUANT_BTC_PAPER_ONLY=false.
        paper_only=True,
        fee_bps=0.0,
        edge_threshold=0.05,
        # 2s tick frequency works for Coinbase (the default source) and
        # gives the rolling-vol calibration ~150 samples per 5m window.
        # If POLYGON_RPC_URL is set so Chainlink becomes the source,
        # consider raising this to 30s to stay under free-tier rate
        # limits (Chainlink heartbeats every ~60s anyway).
        tick_interval_s=2.0,
        slug_token="btc",
        question_keywords=("Bitcoin", "BTC"),
    ),
    "ETH": AssetSpec(
        asset_id="ETH",
        asset_class=AssetClass.CRYPTO,
        price_source=lambda: CoinbaseSpotSource("ETH-USD"),
        settlement_source=lambda: CoinbaseSpotSource("ETH-USD"),
        default_vol=0.75,
        vol_calibration=VolCalibration(
            method=VolMethod.HYBRID,
            fixed_value=0.75,
            hybrid_threshold_s=4 * 3600,
        ),
        supported_market_families=frozenset({MarketFamily.STRIKE, MarketFamily.RANGE}),
        paper_only=False,
        fee_bps=0.0,
        edge_threshold=0.05,
        tick_interval_s=2.0,
        slug_token="eth",
        question_keywords=("Ethereum", "ETH"),
    ),
}


def get(asset_id: str) -> AssetSpec | None:
    return ASSETS.get(asset_id)


def enabled_for(family: MarketFamily) -> list[AssetSpec]:
    return [s for s in ASSETS.values() if family in s.supported_market_families]


def live_eligible(family: MarketFamily) -> list[AssetSpec]:
    return [s for s in enabled_for(family) if not s.paper_only]


def _bool_env(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _float_env(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def apply_env_overrides(spec: AssetSpec) -> AssetSpec:
    """Return a new AssetSpec with QUANT_<ASSET>_* env values applied."""
    a = spec.asset_id
    overrides: dict = {}
    if (v := _float_env(f"QUANT_{a}_VOL")) is not None:
        overrides["default_vol"] = v
        cal = spec.vol_calibration
        if cal.method in (VolMethod.FIXED, VolMethod.HYBRID):
            overrides["vol_calibration"] = replace(cal, fixed_value=v)
    if (v := _float_env(f"QUANT_{a}_EDGE_THRESHOLD")) is not None:
        overrides["edge_threshold"] = v
    if (v := _float_env(f"QUANT_{a}_FEE_BPS")) is not None:
        overrides["fee_bps"] = v
    if (b := _bool_env(f"QUANT_{a}_PAPER_ONLY")) is not None:
        overrides["paper_only"] = b
    if not overrides:
        return spec
    return replace(spec, **overrides)

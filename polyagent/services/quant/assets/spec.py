"""Typed asset specification.

The registry in `registry.py` declares one AssetSpec per supported asset.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from polyagent.services.quant.assets.sources.base import PriceSource, SettlementSource
from polyagent.services.quant.core.vol import VolCalibration


class AssetClass(str, Enum):
    CRYPTO = "CRYPTO"
    FX = "FX"
    COMMODITY = "COMMODITY"


class MarketFamily(str, Enum):
    SHORT_HORIZON = "SHORT_HORIZON"
    STRIKE = "STRIKE"
    RANGE = "RANGE"


PriceSourceFactory = Callable[[], PriceSource]
SettlementSourceFactory = Callable[[], SettlementSource]


@dataclass(frozen=True)
class AssetSpec:
    asset_id: str
    asset_class: AssetClass
    price_source: PriceSourceFactory
    settlement_source: SettlementSourceFactory
    default_vol: float
    vol_calibration: VolCalibration
    supported_market_families: frozenset[MarketFamily]
    paper_only: bool = False
    fee_bps: float = 0.0
    edge_threshold: float = 0.05
    tick_interval_s: float = 2.0
    slug_token: str = ""
    question_keywords: tuple[str, ...] = ()

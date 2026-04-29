"""PriceSource and SettlementSource Protocols.

PriceSource is hot-path tick-frequency. SettlementSource is occasional
historical lookup. They are separate so an asset can have one without the
other (e.g. paper-only with no live ticks).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol


class PriceSource(Protocol):
    def tick(self) -> Decimal | None: ...
    def current(self) -> Decimal | None: ...
    def realized_vol(self, window_s: int) -> float: ...
    def close(self) -> None: ...


class SettlementSource(Protocol):
    def price_at(self, ts: datetime) -> Decimal | None: ...
    def source_id(self) -> str: ...

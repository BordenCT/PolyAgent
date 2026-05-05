"""Realized P&L for binary paper trades.

`size` is USD stake — i.e. the dollars actually paid to acquire the
contracts (= contracts × fill_price). `fill_price` is the per-contract
price in the contract's own coordinate system: YES_ask for YES side,
NO_ask (= 1 - YES_bid) for NO side. A win pays $1 per contract held;
a loss is total stake.
"""
from __future__ import annotations

from decimal import Decimal


def compute_pnl(
    side: str,
    fill_price: Decimal,
    outcome: str,
    size: Decimal,
) -> Decimal:
    """Signed P&L in USD for a binary paper trade.

    contracts = size / fill_price.
    Win:  contracts × (1 - fill_price)  =  size × (1/fill_price - 1)
    Loss: -size  (full stake)
    """
    won = (side == outcome)
    if won:
        return size * (Decimal("1") - fill_price) / fill_price
    return -size

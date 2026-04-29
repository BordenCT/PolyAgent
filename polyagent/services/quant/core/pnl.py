"""Realized P&L for binary paper trades.

`size` is USD notional. YES side profits `(1 - fill_price)` per unit
notional if outcome is YES, loses `fill_price` if NO. NO side mirrors.
"""
from __future__ import annotations

from decimal import Decimal


def compute_pnl(
    side: str,
    fill_price: Decimal,
    outcome: str,
    size: Decimal,
) -> Decimal:
    """Signed P&L in USD for a binary paper trade."""
    if side == "YES":
        return size * (Decimal("1") - fill_price) if outcome == "YES" else -size * fill_price
    return size * (Decimal("1") - fill_price) if outcome == "NO" else -size * fill_price

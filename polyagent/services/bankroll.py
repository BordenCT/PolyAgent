"""Unified bankroll accounting across the main and short-horizon ledgers.

Polymarket account starts at one fixed $X. Both subsystems (the
Claude-brain main bot writing to ``positions``, and the short-horizon
quant decider writing to ``quant_short_trades``) draw from the same pot
in reality. This module is the single source of truth for that pot's
state so both bots can size and gate against the same number.

Usage:
    state = compute_bankroll_state(db, settings.bankroll)
    if state.free < settings.min_free_bankroll:
        ...   # don't enter
    kelly_size = abs(edge) * 0.25 * state.free
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from polyagent.infra.database import Database

_BANKROLL_QUERY = """
    SELECT
        COALESCE((SELECT SUM(pnl) FROM positions WHERE status='closed'), 0)
            AS realized_main,
        COALESCE((SELECT SUM(pnl) FROM quant_short_trades WHERE pnl IS NOT NULL), 0)
            AS realized_quant,
        COALESCE((SELECT SUM(position_size) FROM positions WHERE status='open'), 0)
            AS open_main,
        COALESCE((SELECT SUM(size) FROM quant_short_trades WHERE pnl IS NULL), 0)
            AS open_quant
"""


@dataclass(frozen=True)
class BankrollState:
    """Snapshot of bankroll allocation across both ledgers.

    All amounts in USD. ``free`` is the headline number both decision
    paths gate against and Kelly-size from.
    """

    starting: Decimal
    realized_main: Decimal
    realized_quant: Decimal
    open_capital_main: Decimal
    open_capital_quant: Decimal

    @property
    def realized_total(self) -> Decimal:
        return self.realized_main + self.realized_quant

    @property
    def open_capital_total(self) -> Decimal:
        return self.open_capital_main + self.open_capital_quant

    @property
    def cumulative(self) -> Decimal:
        """``starting + realized_total``: the bankroll's running balance."""
        return self.starting + self.realized_total

    @property
    def free(self) -> Decimal:
        """``cumulative - open_capital_total``: deployable to a new trade."""
        return self.cumulative - self.open_capital_total


def compute_bankroll_state(db: Database, starting) -> BankrollState:
    """Query both ledgers and return a unified bankroll snapshot."""
    with db.cursor() as cur:
        cur.execute(_BANKROLL_QUERY)
        row = cur.fetchone()
    return BankrollState(
        starting=Decimal(str(starting)),
        realized_main=Decimal(str(row["realized_main"])),
        realized_quant=Decimal(str(row["realized_quant"])),
        open_capital_main=Decimal(str(row["open_main"])),
        open_capital_quant=Decimal(str(row["open_quant"])),
    )

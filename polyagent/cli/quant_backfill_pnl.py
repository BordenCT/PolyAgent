"""Backfill historical short-horizon trades from old-format to new-format.

The fixes in commit c6c9e44 changed two on-row conventions:

1. NO-side ``fill_price_assumed`` now stores the NO_ask (= 1 - YES_bid),
   not the YES_bid. Previously the recorded "fill" was in YES coords for
   NO trades, and the recorded ``size`` (= contracts × YES_bid) was the
   capital we *would* have collected if shorting YES at the bid, NOT
   the actual stake paid to buy NO contracts.

2. ``compute_pnl`` now reads ``size`` as USD stake (matching how the
   decider writes it and how :func:`compute_bankroll_state` sums it) and
   computes win = ``size * (1 - fill) / fill``, loss = ``-size``.

Trades resolved before c6c9e44 were written under the old conventions,
so their (fill, size, pnl) triples are inconsistent with the new code.
This command rewrites them in place.

Operation per legacy trade:
  YES side: fill and size are unchanged (they were already in the side's
  own coordinate system and were already the actual stake). Only ``pnl``
  is recomputed under the new formula.
  NO side: ``new_fill = 1 - old_fill``;
  ``new_size = old_size * (1 - old_fill) / old_fill``
  (= contracts × NO_ask, the actual stake). The contract count itself
  is invariant: ``old_size/old_fill == new_size/new_fill``. ``pnl`` is
  then recomputed under the new formula.

Destructive: overwrites historical ``fill_price_assumed``, ``size``, and
``pnl`` columns. Preview with the default dry-run; pass ``--confirm`` to
write. Each market is rewritten in a single transaction so a partial
failure cannot leave a market in a half-converted state.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from polyagent.infra.config import Settings
from polyagent.infra.database import Database
from polyagent.services.quant.core.pnl import compute_pnl


SELECT_RESOLVED_TRADES = """
    SELECT t.id, t.market_id, t.side,
           t.fill_price_assumed, t.size, t.pnl,
           m.outcome, m.slug
    FROM quant_short_trades t
    JOIN quant_short_markets m ON m.id = t.market_id
    WHERE t.pnl IS NOT NULL
      AND m.outcome IS NOT NULL
      AND (%(asset)s::text IS NULL OR m.asset_id = %(asset)s)
    ORDER BY t.resolved_at NULLS LAST
    LIMIT %(limit)s
"""

UPDATE_TRADE = """
    UPDATE quant_short_trades
    SET fill_price_assumed = %(fill_price_assumed)s,
        size = %(size)s,
        pnl = %(pnl)s
    WHERE id = %(id)s
"""


@dataclass(frozen=True)
class LegacyRowFix:
    """The post-transform values for a legacy trade row.

    The transform itself is pure (no DB). Caller decides whether to
    write these back.
    """
    new_fill: Decimal
    new_size: Decimal
    new_pnl: Decimal


def _old_pnl(side: str, fill: Decimal, size: Decimal, outcome: str) -> Decimal:
    """The pnl that the old compute_pnl would have produced.

    Used only for legacy detection — never to write new values.
    """
    won = (side == outcome)
    return (
        size * (Decimal("1") - fill)
        if won
        else -size * fill
    )


def is_already_new_format(row: dict) -> bool:
    """Return True if ``row``'s stored pnl matches the *new* formula.

    Trades resolved after c6c9e44 will return True (no-op). Legacy
    trades resolved before will return False.
    """
    side = row["side"]
    fill = Decimal(str(row["fill_price_assumed"]))
    size = Decimal(str(row["size"]))
    outcome = row["outcome"]
    stored = Decimal(str(row["pnl"]))
    expected_new = compute_pnl(side, fill, outcome, size)
    return stored == expected_new


def legacy_to_new(row: dict) -> LegacyRowFix:
    """Convert one legacy trade row to its new-format equivalent.

    For YES rows: fill and size are unchanged (the YES ask was already
    the actual per-contract price, and contracts × ask was already the
    actual stake). Only pnl is recomputed under the new formula.

    For NO rows: ``new_fill = 1 - old_fill`` (YES_bid → NO_ask) and
    ``new_size = old_size × (1 - old_fill) / old_fill`` (so that the
    underlying contract count ``size/fill`` is invariant). pnl is then
    recomputed under the new formula.
    """
    side = row["side"]
    fill = Decimal(str(row["fill_price_assumed"]))
    size = Decimal(str(row["size"]))
    outcome = row["outcome"]

    if side == "NO":
        new_fill = (Decimal("1") - fill).quantize(Decimal("0.0001"))
        new_size = (size * (Decimal("1") - fill) / fill).quantize(Decimal("0.01"))
    else:
        new_fill = fill
        new_size = size

    new_pnl = compute_pnl(side, new_fill, outcome, new_size).quantize(Decimal("0.01"))
    return LegacyRowFix(new_fill=new_fill, new_size=new_size, new_pnl=new_pnl)


@click.command("quant-backfill-pnl")
@click.option("--asset", type=str, default=None,
              help="Filter to a single asset_id (e.g. BTC).")
@click.option("--limit", type=int, default=10_000, show_default=True,
              help="Max number of resolved trades to inspect.")
@click.option("--confirm", is_flag=True,
              help="Actually rewrite fill/size/pnl. Without this, runs as dry-run.")
@click.option("--dry-run", is_flag=True,
              help="Explicitly preview without writing. Default behavior when --confirm is omitted.")
@click.option("--show", type=int, default=20, show_default=True,
              help="Max rows of detail to print in the diff table.")
def quant_backfill_pnl(
    asset: Optional[str],
    limit: int,
    confirm: bool,
    dry_run: bool,
    show: int,
) -> None:
    """Rewrite legacy short-horizon trades into the post-c6c9e44 format.

    Without ``--confirm`` the command runs as a preview: it identifies
    legacy rows and prints the (old → new) diff but writes nothing.
    Pass ``--confirm`` to apply.
    """
    if confirm and dry_run:
        click.echo("--confirm and --dry-run are mutually exclusive.", err=True)
        sys.exit(1)
    write_mode = confirm and not dry_run

    console = Console()
    settings = Settings.from_env()
    db = Database(settings)

    try:
        with db.cursor() as cur:
            cur.execute(SELECT_RESOLVED_TRADES, {"asset": asset, "limit": limit})
            rows = cur.fetchall()

        if not rows:
            console.print("[yellow]No resolved trades found.[/yellow]")
            return

        legacy_rows: list[tuple[dict, LegacyRowFix]] = []
        n_already_new = 0
        for r in rows:
            if is_already_new_format(r):
                n_already_new += 1
                continue
            legacy_rows.append((r, legacy_to_new(r)))

        mode_label = "WRITE" if write_mode else "DRY-RUN"
        console.print(
            f"[cyan]{mode_label}[/cyan] backfill: {len(rows)} settled trades inspected"
            f"{f' for asset={asset}' if asset else ''}, "
            f"{len(legacy_rows)} legacy / {n_already_new} already-new."
        )

        if legacy_rows and show > 0:
            t = Table(
                title=f"{'Rewriting' if write_mode else 'Would rewrite'} (showing first {min(show, len(legacy_rows))})"
            )
            t.add_column("Slug", style="cyan", overflow="fold", max_width=36)
            t.add_column("Side")
            t.add_column("Fill: was → now")
            t.add_column("Size: was → now")
            t.add_column("P&L: was → now", justify="right")
            for row, fix in legacy_rows[:show]:
                old_pnl = Decimal(str(row["pnl"]))
                pnl_delta = fix.new_pnl - old_pnl
                style = "green" if pnl_delta >= 0 else "red"
                t.add_row(
                    row["slug"], row["side"],
                    f"{Decimal(str(row['fill_price_assumed']))} → {fix.new_fill}",
                    f"${Decimal(str(row['size']))} → ${fix.new_size}",
                    f"${old_pnl:+.2f} → [{style}]${fix.new_pnl:+.2f}[/{style}]",
                )
            console.print(t)

        if write_mode and legacy_rows:
            # Group by market_id so each market's trades flip atomically.
            by_market: dict = {}
            for row, fix in legacy_rows:
                by_market.setdefault(row["market_id"], []).append((row, fix))
            for market_id, items in by_market.items():
                with db.connection() as conn:
                    with conn.transaction():
                        with conn.cursor() as cur:
                            for row, fix in items:
                                cur.execute(UPDATE_TRADE, {
                                    "id": row["id"],
                                    "fill_price_assumed": fix.new_fill,
                                    "size": fix.new_size,
                                    "pnl": fix.new_pnl,
                                })

        total_old = sum((Decimal(str(r["pnl"])) for r, _ in legacy_rows), Decimal("0"))
        total_new = sum((f.new_pnl for _, f in legacy_rows), Decimal("0"))
        delta = total_new - total_old

        summary = Table(title="Backfill summary")
        summary.add_column("Metric", style="cyan")
        summary.add_column("Value")
        summary.add_row("Mode", "WRITE" if write_mode else "DRY-RUN (no writes)")
        summary.add_row("Trades inspected", str(len(rows)))
        summary.add_row(
            "Legacy trades",
            f"[yellow]{len(legacy_rows)}[/yellow]" if legacy_rows else "[green]0[/green]",
        )
        summary.add_row("Already in new format", str(n_already_new))
        summary.add_row("Total P&L (old format)", f"${total_old:+.2f}")
        summary.add_row("Total P&L (new format)", f"${total_new:+.2f}")
        delta_style = "green" if delta >= 0 else "red"
        summary.add_row(
            "Aggregate P&L correction",
            f"[{delta_style}]${delta:+.2f}[/{delta_style}]",
        )
        console.print(summary)

        if not write_mode and legacy_rows:
            console.print(
                "\n[yellow]Re-run with --confirm to apply these writes.[/yellow]"
            )

    finally:
        db.close()

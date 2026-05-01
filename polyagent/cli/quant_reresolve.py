"""Re-resolve already-resolved short-horizon markets against Polymarket truth.

The previous resolver computed outcomes by comparing start_spot to end_spot
from a Coinbase settlement source. ``quant-validate`` showed ~43% of those
outcomes disagreed with Polymarket and the bias was consistently positive,
inflating the books with phantom wins. The fix to use Polymarket as the
source of truth (commit d0c21ed) only applies to *new* resolutions; this
command rewrites historical rows so the books retroactively reconcile.

Operation per market:
1. Fetch the market's actual settled outcome from Polymarket.
2. Skip when Polymarket agrees with our recorded outcome (no-op).
3. Skip when Polymarket itself has not yet resolved (defer).
4. When PM disagrees: recompute pnl for every trade on that market using
   :func:`compute_pnl` against the PM outcome, then update both the
   ``quant_short_markets.outcome`` row and the affected ``pnl`` rows.

Destructive: overwrites historical ``outcome`` and ``pnl`` columns. Use
``--dry-run`` to preview the diff before passing ``--confirm``.

Each market is rewritten in a single transaction so a partial failure
mid-iteration cannot leave an outcome flipped while its trades still
hold the old pnl.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from polyagent.data.clients.polymarket import PolymarketClient
from polyagent.infra.config import Settings
from polyagent.infra.database import Database
from polyagent.services.quant.core.pnl import compute_pnl

_PM_PRICE_SOURCE_ID = "polymarket:clob"

SELECT_RESOLVED_MARKETS = """
    SELECT id, polymarket_id, slug, asset_id, outcome
    FROM quant_short_markets
    WHERE outcome IS NOT NULL
      AND (%(asset)s::text IS NULL OR asset_id = %(asset)s)
    ORDER BY resolved_at DESC NULLS LAST
    LIMIT %(limit)s
"""

SELECT_TRADES_FOR_MARKET = """
    SELECT id, side, fill_price_assumed, size, pnl
    FROM quant_short_trades
    WHERE market_id = %(market_id)s
"""

# Update outcome / price_source_id only; do NOT touch resolved_at,
# start_spot, end_spot. Backfill should not rewrite the original
# settlement-time audit fields.
UPDATE_MARKET_OUTCOME = """
    UPDATE quant_short_markets
    SET outcome = %(outcome)s,
        price_source_id = %(price_source_id)s
    WHERE id = %(id)s
"""

# Update pnl only; do NOT touch resolved_at. Same reason.
UPDATE_TRADE_PNL = """
    UPDATE quant_short_trades
    SET pnl = %(pnl)s
    WHERE id = %(id)s
"""


@dataclass(frozen=True)
class MarketChange:
    slug: str
    old_outcome: str
    new_outcome: str
    pnl_delta: Decimal


def _pm_outcome(state: Optional[dict]) -> Optional[str]:
    """Decode Polymarket state to YES / NO / None (defer)."""
    if state is None or not state.get("is_resolved"):
        return None
    midpoint = state.get("midpoint_price")
    if midpoint == Decimal("1"):
        return "YES"
    if midpoint == Decimal("0"):
        return "NO"
    return None


def _market_pnl_delta(trades: list[dict], pm_outcome: str) -> tuple[Decimal, list[tuple[str, Decimal]]]:
    """Compute (sum_delta, [(trade_id, new_pnl)]) for one market under PM's outcome.

    Pure function. Caller decides whether to actually write the new pnls.
    """
    total = Decimal("0")
    updates: list[tuple[str, Decimal]] = []
    for t in trades:
        old_pnl = Decimal(str(t["pnl"]))
        new_pnl = compute_pnl(
            t["side"],
            Decimal(str(t["fill_price_assumed"])),
            pm_outcome,
            Decimal(str(t["size"])),
        )
        total += (new_pnl - old_pnl)
        updates.append((t["id"], new_pnl))
    return total, updates


@click.command("quant-reresolve")
@click.option("--asset", type=str, default=None,
              help="Filter to a single asset_id (e.g. BTC).")
@click.option("--limit", type=int, default=10_000, show_default=True,
              help="Max number of resolved markets to inspect.")
@click.option("--confirm", is_flag=True,
              help="Actually rewrite outcomes and pnl. Without this, runs as dry-run.")
@click.option("--dry-run", is_flag=True,
              help="Explicitly preview without writing. Default behavior when --confirm is omitted.")
def quant_reresolve(
    asset: Optional[str],
    limit: int,
    confirm: bool,
    dry_run: bool,
) -> None:
    """Backfill historical short-horizon outcomes against Polymarket truth.

    Without ``--confirm`` the command operates in preview mode: it prints
    every market that would change but writes nothing. Pass ``--confirm``
    to make the writes.
    """
    if confirm and dry_run:
        click.echo("--confirm and --dry-run are mutually exclusive.", err=True)
        sys.exit(1)
    write_mode = confirm and not dry_run

    console = Console()
    settings = Settings.from_env()
    db = Database(settings)
    client = PolymarketClient()

    try:
        with db.cursor() as cur:
            cur.execute(SELECT_RESOLVED_MARKETS, {"asset": asset, "limit": limit})
            markets = cur.fetchall()

        if not markets:
            console.print("[yellow]No resolved markets found.[/yellow]")
            return

        mode_label = "WRITE" if write_mode else "DRY-RUN"
        console.print(
            f"[cyan]{mode_label}[/cyan] re-resolving {len(markets)} markets"
            f"{f' for asset={asset}' if asset else ''}..."
        )

        changes: list[MarketChange] = []
        n_unchanged = 0
        n_pm_unresolved = 0
        n_pm_unknown_midpoint = 0
        n_fetch_failed = 0

        for m in markets:
            try:
                state = client.fetch_market_state(m["polymarket_id"])
            except Exception as exc:
                n_fetch_failed += 1
                console.print(
                    f"[yellow]warn:[/yellow] PM fetch failed for {m['slug']}: {exc}"
                )
                continue

            pm_o = _pm_outcome(state)
            if pm_o is None:
                if state is None or not state.get("is_resolved"):
                    n_pm_unresolved += 1
                else:
                    n_pm_unknown_midpoint += 1
                continue

            if pm_o == m["outcome"]:
                n_unchanged += 1
                continue

            with db.cursor() as cur:
                cur.execute(SELECT_TRADES_FOR_MARKET, {"market_id": m["id"]})
                trades = cur.fetchall()

            delta, trade_updates = _market_pnl_delta(trades, pm_o)
            change = MarketChange(
                slug=m["slug"], old_outcome=m["outcome"],
                new_outcome=pm_o, pnl_delta=delta,
            )
            changes.append(change)

            if write_mode:
                # One transaction per market: outcome + all trade pnls
                # flip together or not at all. psycopg's conn.transaction()
                # commits on success and rolls back on exception.
                with db.connection() as conn:
                    with conn.transaction():
                        with conn.cursor() as cur:
                            cur.execute(UPDATE_MARKET_OUTCOME, {
                                "id": m["id"],
                                "outcome": pm_o,
                                "price_source_id": _PM_PRICE_SOURCE_ID,
                            })
                            for trade_id, new_pnl in trade_updates:
                                cur.execute(UPDATE_TRADE_PNL, {
                                    "id": trade_id, "pnl": new_pnl,
                                })

        if changes:
            change_table = Table(
                title=f"{'Rewritten' if write_mode else 'Would rewrite'} markets ({len(changes)})"
            )
            change_table.add_column("Slug", style="cyan", overflow="fold", max_width=42)
            change_table.add_column("Was")
            change_table.add_column("Now")
            change_table.add_column("Pnl delta", justify="right")
            for c in changes:
                style = "green" if c.pnl_delta >= 0 else "red"
                change_table.add_row(
                    c.slug, c.old_outcome, c.new_outcome,
                    f"[{style}]${c.pnl_delta:+.2f}[/{style}]",
                )
            console.print(change_table)

        total_delta = sum((c.pnl_delta for c in changes), Decimal("0"))
        summary = Table(title="Re-resolve summary")
        summary.add_column("Metric", style="cyan")
        summary.add_column("Value")
        summary.add_row("Mode", "WRITE" if write_mode else "DRY-RUN (no writes)")
        summary.add_row("Markets inspected", str(len(markets)))
        summary.add_row(
            "Outcomes flipped",
            f"[red]{len(changes)}[/red]" if changes else "[green]0[/green]",
        )
        summary.add_row("Outcomes already correct", str(n_unchanged))
        summary.add_row("PM not yet resolved", str(n_pm_unresolved))
        if n_pm_unknown_midpoint:
            summary.add_row(
                "PM resolved with unknown midpoint",
                f"[yellow]{n_pm_unknown_midpoint}[/yellow]",
            )
        if n_fetch_failed:
            summary.add_row("Fetch failures", f"[yellow]{n_fetch_failed}[/yellow]")
        delta_style = "green" if total_delta >= 0 else "red"
        summary.add_row(
            "Aggregate P&L correction",
            f"[{delta_style}]${total_delta:+.2f}[/{delta_style}]",
        )
        console.print(summary)

        if not write_mode and changes:
            console.print(
                "\n[yellow]Re-run with --confirm to apply these writes.[/yellow]"
            )

    finally:
        client.close()
        db.close()

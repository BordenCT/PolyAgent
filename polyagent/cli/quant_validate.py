"""Validate resolved short-horizon trades against P&L math and Polymarket truth.

Two independent checks per trade:

1. **Math self-check** (no network): recompute `pnl` from
   ``(side, fill_price_assumed, outcome, size)`` via :func:`compute_pnl`
   and compare to the stored value. Catches P&L formula bugs and DB
   corruption. Free to run.

2. **Polymarket cross-check** (HTTP): for each unique market, fetch
   Polymarket's actual resolution and compare to our recorded ``outcome``.
   Catches settlement-source divergence (Coinbase vs Binance noon),
   outcome-direction errors (e.g. flipped >= comparison), and any
   Polymarket re-settlements after our resolver wrote.

The summary also computes the dollar bias between recorded P&L and the
P&L we *would* have under Polymarket's outcomes; a non-zero bias means
some "wins" on the books would have been losses (or vice versa) in the
real market.
"""
from __future__ import annotations

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

SELECT_RESOLVED = """
    SELECT
        trade_id,
        polymarket_id,
        slug,
        asset_id,
        window_minutes,
        side,
        fill_price_assumed,
        size,
        outcome              AS our_outcome,
        pnl                  AS our_pnl,
        trade_resolved_at,
        price_source_id
    FROM quant_short_v
    WHERE pnl IS NOT NULL
      AND (%(asset)s::text IS NULL OR asset_id = %(asset)s)
    ORDER BY trade_resolved_at DESC
    LIMIT %(limit)s
"""


@dataclass(frozen=True)
class RowVerdict:
    """One trade's validation result.

    Attributes:
        math_mismatch: stored ``pnl`` differs from re-computed pnl using the
            stored ``outcome``. Indicates a P&L formula bug or DB corruption.
        pm_outcome: Polymarket's reported outcome (``YES``/``NO``), or None
            if Polymarket has not yet resolved or the call was skipped.
        pm_mismatch: our recorded ``outcome`` differs from ``pm_outcome``.
        corrected_pnl: P&L the trade *would* have had under Polymarket's
            outcome. Equal to ``recorded_pnl`` when there's no PM mismatch
            and the math is correct. None when PM hasn't resolved.
        recorded_pnl: stored ``pnl`` from the DB.
    """

    math_mismatch: bool
    pm_outcome: Optional[str]
    pm_mismatch: bool
    corrected_pnl: Optional[Decimal]
    recorded_pnl: Decimal


def validate_row(row: dict, pm_state: Optional[dict]) -> RowVerdict:
    """Compute math + Polymarket verdicts for one trade row.

    Pure function so it's unit-testable without a DB or HTTP client.

    Args:
        row: Dict from ``quant_short_v`` with at least ``side``,
            ``fill_price_assumed``, ``our_outcome``, ``size``, ``our_pnl``.
        pm_state: Result of :meth:`PolymarketClient.fetch_market_state`,
            or None when network was skipped or the call failed. A state
            dict with ``is_resolved=False`` is treated identically to None
            for outcome comparison.
    """
    side = row["side"]
    fill = Decimal(str(row["fill_price_assumed"]))
    size = Decimal(str(row["size"]))
    our_outcome = row["our_outcome"]
    recorded = Decimal(str(row["our_pnl"]))

    math_pnl = compute_pnl(side, fill, our_outcome, size)
    math_mismatch = recorded != math_pnl

    pm_outcome: Optional[str] = None
    pm_mismatch = False
    corrected: Optional[Decimal] = None
    if pm_state and pm_state.get("is_resolved"):
        midpoint = pm_state.get("midpoint_price")
        if midpoint == Decimal("1"):
            pm_outcome = "YES"
        elif midpoint == Decimal("0"):
            pm_outcome = "NO"
        if pm_outcome is not None:
            corrected = compute_pnl(side, fill, pm_outcome, size)
            pm_mismatch = pm_outcome != our_outcome

    return RowVerdict(
        math_mismatch=math_mismatch,
        pm_outcome=pm_outcome,
        pm_mismatch=pm_mismatch,
        corrected_pnl=corrected,
        recorded_pnl=recorded,
    )


@click.command("quant-validate")
@click.option("--limit", type=int, default=50, show_default=True,
              help="Max number of recent resolved trades to inspect.")
@click.option("--asset", type=str, default=None,
              help="Filter to a single asset_id (e.g. BTC).")
@click.option("--no-network", is_flag=True,
              help="Skip the Polymarket cross-check; run only the math self-check.")
@click.option("--mismatches-only", is_flag=True,
              help="Only print rows where math or Polymarket disagrees.")
def quant_validate(
    limit: int,
    asset: Optional[str],
    no_network: bool,
    mismatches_only: bool,
) -> None:
    """Cross-check resolved short-horizon trades against P&L math and Polymarket."""
    console = Console()
    settings = Settings.from_env()
    db = Database(settings)
    client: Optional[PolymarketClient] = None if no_network else PolymarketClient()

    try:
        with db.cursor() as cur:
            cur.execute(SELECT_RESOLVED, {"asset": asset, "limit": limit})
            rows = cur.fetchall()

        if not rows:
            console.print("[yellow]No resolved trades to validate.[/yellow]")
            return

        # Many trades can share one market; dedupe network calls.
        pm_states: dict[str, Optional[dict]] = {}
        if client is not None:
            unique_markets = {r["polymarket_id"] for r in rows}
            for pm_id in unique_markets:
                try:
                    pm_states[pm_id] = client.fetch_market_state(pm_id)
                except Exception as exc:
                    pm_states[pm_id] = None
                    console.print(f"[yellow]warn: PM fetch failed for {pm_id}: {exc}[/yellow]")

        table = Table(title=f"Validation of {len(rows)} most recent resolved trades")
        table.add_column("Slug", style="cyan", overflow="fold", max_width=42)
        table.add_column("Side")
        table.add_column("Our")
        table.add_column("PM")
        table.add_column("Recorded P&L", justify="right")
        table.add_column("True P&L", justify="right")
        table.add_column("Verdict")

        n_math_bad = 0
        n_pm_known = 0
        n_pm_bad = 0
        sum_recorded = Decimal("0")
        sum_corrected = Decimal("0")

        for r in rows:
            v = validate_row(r, pm_states.get(r["polymarket_id"]))
            if v.math_mismatch:
                n_math_bad += 1
            if v.pm_outcome is not None:
                n_pm_known += 1
                if v.pm_mismatch:
                    n_pm_bad += 1
            sum_recorded += v.recorded_pnl
            if v.corrected_pnl is not None:
                sum_corrected += v.corrected_pnl

            tags = []
            if v.math_mismatch:
                tags.append("MATH")
            if v.pm_mismatch:
                tags.append("PM")
            verdict = "[red]" + ",".join(tags) + "[/red]" if tags else "[green]ok[/green]"
            if mismatches_only and not tags:
                continue

            true_pnl_str = f"${v.corrected_pnl:+.2f}" if v.corrected_pnl is not None else "-"
            table.add_row(
                str(r["slug"]),
                str(r["side"]),
                str(r["our_outcome"]),
                v.pm_outcome or "?",
                f"${v.recorded_pnl:+.2f}",
                true_pnl_str,
                verdict,
            )

        console.print(table)

        summary = Table(title="Validation summary")
        summary.add_column("Check", style="cyan")
        summary.add_column("Result")
        summary.add_row("Trades inspected", str(len(rows)))
        summary.add_row(
            "Math self-check",
            f"[red]{n_math_bad} of {len(rows)} mismatch[/red]" if n_math_bad
            else "[green]all match[/green]",
        )
        if client is not None:
            summary.add_row(
                "Polymarket cross-check",
                f"[red]{n_pm_bad} of {n_pm_known} disagree[/red]"
                if n_pm_bad
                else f"[green]all {n_pm_known} agree[/green]",
            )
            summary.add_row("Unresolved on PM", str(len(rows) - n_pm_known))
            if n_pm_known:
                bias = sum_recorded - sum_corrected
                # Only the rows we could check contribute to the bias number.
                bias_style = "green" if bias == 0 else "red"
                summary.add_row(
                    "Recorded vs True P&L bias",
                    f"[{bias_style}]${bias:+.2f}[/{bias_style}] over {n_pm_known} trades",
                )
        else:
            summary.add_row("Polymarket cross-check", "skipped (--no-network)")
        console.print(summary)

    finally:
        if client is not None:
            client.close()
        db.close()

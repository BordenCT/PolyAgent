"""buy-review command — surface thesis records for losing closed BUYs."""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import click

from polyagent.infra.config import Settings
from polyagent.infra.database import Database


QUERY = """
    SELECT
        p.id              AS position_id,
        p.side,
        p.entry_price,
        p.target_price,
        p.position_size,
        p.pnl,
        p.exit_reason,
        p.opened_at,
        p.closed_at,
        m.question,
        m.market_class::text AS market_class,
        t.claude_estimate,
        t.confidence,
        t.checks,
        t.checks_passed,
        t.consensus,
        t.thesis_text
    FROM positions p
    JOIN markets m ON p.market_id = m.id
    JOIN thesis  t ON p.thesis_id = t.id
    WHERE p.status = 'closed'
      AND p.side = %(side)s
      AND p.pnl < 0
    ORDER BY p.closed_at DESC
    LIMIT %(limit)s
"""


def _json_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    raise TypeError(f"Type {type(obj).__name__} not JSON serializable")


@click.command("buy-review")
@click.option("--side", type=click.Choice(["BUY", "SELL"]), default="BUY", show_default=True)
@click.option("-n", "--limit", type=int, default=10, show_default=True, help="Number of losing positions to fetch")
@click.option("--jsonl", "as_jsonl", is_flag=True, help="Emit one JSON object per line")
def buy_review(side: str, limit: int, as_jsonl: bool):
    """Show thesis details for losing closed positions on the given side.

    Default: 10 most recent losing BUYs with the brain's prediction, confidence,
    checks, and reasoning text. Use --jsonl for a paste-safe export.
    """
    settings = Settings.from_env()
    db = Database(settings)
    with db.cursor() as cur:
        cur.execute(QUERY, {"side": side, "limit": limit})
        rows = cur.fetchall()
    db.close()

    if as_jsonl:
        for r in rows:
            click.echo(json.dumps(dict(r), default=_json_default))
        return

    for r in rows:
        click.echo("=" * 80)
        click.echo(f"{r['question']}  [{r['market_class']}]")
        click.echo(
            f"  side={r['side']}  entry=${float(r['entry_price']):.4f}  "
            f"target=${float(r['target_price']):.4f}  "
            f"size=${float(r['position_size']):.2f}  pnl=${float(r['pnl']):+.2f}  "
            f"exit={r['exit_reason']}"
        )
        click.echo(
            f"  claude_estimate={float(r['claude_estimate']):.2%}  "
            f"confidence={float(r['confidence']):.2%}  "
            f"checks_passed={r['checks_passed']}  consensus={r['consensus']}"
        )
        click.echo(f"  checks={r['checks']}")
        text = (r["thesis_text"] or "").strip()
        if text:
            click.echo("  thesis:")
            for line in text.splitlines():
                click.echo(f"    {line}")
        click.echo()

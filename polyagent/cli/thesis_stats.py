"""thesis-stats command — verify the brain's checks/consensus gate is filtering."""
from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

from polyagent.infra.config import Settings
from polyagent.infra.database import Database


GATE_DIST = """
    SELECT checks_passed, consensus, COUNT(*) AS n
    FROM thesis
    GROUP BY 1, 2
    ORDER BY 3 DESC
"""

GATE_BY_OUTCOME = """
    SELECT
        t.checks_passed,
        t.consensus,
        p.side,
        COUNT(*)                                      AS trades,
        COUNT(*) FILTER (WHERE p.pnl > 0)             AS wins,
        COALESCE(SUM(p.pnl), 0)                       AS total_pnl
    FROM thesis t
    JOIN positions p ON p.thesis_id = t.id
    WHERE p.status = 'closed'
    GROUP BY 1, 2, 3
    ORDER BY 1, 2, 3
"""


@click.command("thesis-stats")
def thesis_stats():
    """Show distribution of brain gate outcomes (checks_passed × consensus).

    If almost everything is in a single bucket (e.g. 4 / half), the gate is
    not filtering — it's a rubber stamp.
    """
    console = Console()
    settings = Settings.from_env()
    db = Database(settings)

    with db.cursor() as cur:
        cur.execute(GATE_DIST)
        dist = cur.fetchall()
        cur.execute(GATE_BY_OUTCOME)
        by_outcome = cur.fetchall()
    db.close()

    total = sum(r["n"] for r in dist) or 1
    t1 = Table(title="Thesis gate distribution (all theses)")
    t1.add_column("checks_passed", justify="right")
    t1.add_column("consensus")
    t1.add_column("n", justify="right")
    t1.add_column("%", justify="right")
    for r in dist:
        t1.add_row(str(r["checks_passed"]), r["consensus"], str(r["n"]), f"{r['n'] / total * 100:.1f}%")
    console.print(t1)

    t2 = Table(title="Closed-position outcome by gate")
    t2.add_column("checks_passed", justify="right")
    t2.add_column("consensus")
    t2.add_column("side")
    t2.add_column("trades", justify="right")
    t2.add_column("W%", justify="right")
    t2.add_column("PnL", justify="right")
    for r in by_outcome:
        n = int(r["trades"]) or 1
        wp = int(r["wins"]) / n * 100
        t2.add_row(
            str(r["checks_passed"]),
            r["consensus"],
            r["side"],
            str(r["trades"]),
            f"{wp:.0f}%",
            f"${float(r['total_pnl']):+.2f}",
        )
    console.print(t2)

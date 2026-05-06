"""Quant short-horizon paper-trading analytics: `polyagent quant-stats`."""
from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

from polyagent.infra.config import Settings
from polyagent.infra.database import Database


# All queries read from the `quant_short_v` view (see migration 007), which
# pre-joins quant_short_trades + quant_short_markets and exposes derived
# fields (`abs_edge`, `won`, `window_minutes`). Adding a new analytic just
# means selecting from the view, not modifying tables or repeating joins.
STATS_QUERY_TOTAL = """
    SELECT
        COUNT(*)                            AS trades,
        COUNT(*) FILTER (WHERE won)         AS wins,
        COUNT(*) FILTER (WHERE NOT won)     AS losses,
        COALESCE(AVG(abs_edge), 0)          AS avg_edge,
        COALESCE(SUM(pnl), 0)               AS total_pnl,
        COALESCE(AVG(pnl), 0)               AS avg_pnl,
        COALESCE(AVG(vol_at_decision), 0)   AS avg_vol,
        COALESCE(SUM(size), 0)              AS total_staked,
        COALESCE(AVG(size), 0)              AS avg_size,
        COALESCE(AVG(size / NULLIF(fill_price_assumed, 0)), 0) AS avg_contracts,
        AVG(brier)                          AS avg_brier
    FROM quant_short_v
    WHERE pnl IS NOT NULL
      AND (%(asset)s::text IS NULL OR asset_id = %(asset)s)
"""

STATS_QUERY_BY_DURATION = """
    SELECT
        window_duration_s,
        COUNT(*)                            AS trades,
        COUNT(*) FILTER (WHERE won)         AS wins,
        COUNT(*) FILTER (WHERE NOT won)     AS losses,
        COALESCE(AVG(abs_edge), 0)          AS avg_edge,
        COALESCE(SUM(pnl), 0)               AS total_pnl,
        COALESCE(AVG(pnl), 0)               AS avg_pnl,
        COALESCE(SUM(size), 0)              AS total_staked,
        COALESCE(AVG(size / NULLIF(fill_price_assumed, 0)), 0) AS avg_contracts,
        AVG(brier)                          AS avg_brier
    FROM quant_short_v
    WHERE pnl IS NOT NULL
      AND (%(asset)s::text IS NULL OR asset_id = %(asset)s)
    GROUP BY window_duration_s
    ORDER BY window_duration_s
"""

STATS_QUERY_BY_ASSET = """
    SELECT
        asset_id,
        COUNT(*)                            AS trades,
        COUNT(*) FILTER (WHERE won)         AS wins,
        COUNT(*) FILTER (WHERE NOT won)     AS losses,
        COALESCE(AVG(abs_edge), 0)          AS avg_edge,
        COALESCE(SUM(pnl), 0)               AS total_pnl,
        COALESCE(AVG(pnl), 0)               AS avg_pnl,
        COALESCE(SUM(size), 0)              AS total_staked,
        COALESCE(AVG(size / NULLIF(fill_price_assumed, 0)), 0) AS avg_contracts,
        AVG(brier)                          AS avg_brier
    FROM quant_short_v
    WHERE pnl IS NOT NULL
    GROUP BY asset_id
    ORDER BY asset_id
"""

# Calibration: does win-rate rise with |edge|? If yes, the model has signal
# and the edge_threshold may just be too close to costs. If win% is flat
# across all buckets (~50%), the model has no real signal and tuning the
# threshold won't help — calibration of vol or model choice is the issue.
STATS_QUERY_BY_EDGE = """
    SELECT
        CASE
            WHEN abs_edge < 0.10 THEN '0.05-0.10'
            WHEN abs_edge < 0.15 THEN '0.10-0.15'
            WHEN abs_edge < 0.20 THEN '0.15-0.20'
            ELSE '0.20+'
        END AS edge_bucket,
        CASE
            WHEN abs_edge < 0.10 THEN 1
            WHEN abs_edge < 0.15 THEN 2
            WHEN abs_edge < 0.20 THEN 3
            ELSE 4
        END AS bucket_order,
        COUNT(*)                            AS trades,
        COUNT(*) FILTER (WHERE won)         AS wins,
        COUNT(*) FILTER (WHERE NOT won)     AS losses,
        COALESCE(AVG(abs_edge), 0)          AS avg_edge,
        COALESCE(SUM(pnl), 0)               AS total_pnl,
        COALESCE(AVG(pnl), 0)               AS avg_pnl,
        COALESCE(SUM(size), 0)              AS total_staked,
        COALESCE(AVG(size / NULLIF(fill_price_assumed, 0)), 0) AS avg_contracts,
        AVG(brier)                          AS avg_brier
    FROM quant_short_v
    WHERE pnl IS NOT NULL
      AND (%(asset)s::text IS NULL OR asset_id = %(asset)s)
    GROUP BY edge_bucket, bucket_order
    ORDER BY bucket_order
"""

# Side bias: YES vs NO win rates should be roughly equal under a fair
# directional model. Persistent skew suggests routing/pricing asymmetry
# (e.g., NO-side fills systematically worse, or vol drift in one direction).
STATS_QUERY_BY_SIDE = """
    SELECT
        side,
        COUNT(*)                            AS trades,
        COUNT(*) FILTER (WHERE won)         AS wins,
        COUNT(*) FILTER (WHERE NOT won)     AS losses,
        COALESCE(AVG(abs_edge), 0)          AS avg_edge,
        COALESCE(SUM(pnl), 0)               AS total_pnl,
        COALESCE(AVG(pnl), 0)               AS avg_pnl,
        COALESCE(SUM(size), 0)              AS total_staked,
        COALESCE(AVG(size / NULLIF(fill_price_assumed, 0)), 0) AS avg_contracts,
        AVG(brier)                          AS avg_brier
    FROM quant_short_v
    WHERE pnl IS NOT NULL
      AND (%(asset)s::text IS NULL OR asset_id = %(asset)s)
    GROUP BY side
    ORDER BY side
"""

# Vol regime: rolling realized vol can collapse during quiet stretches,
# making Phi(d2) overconfident and manufacturing fake edges. Buckets are
# wide because realized vol on Coinbase BTC clusters around 0.3–0.7
# annualized; tail buckets catch genuinely extreme regimes.
STATS_QUERY_BY_VOL = """
    SELECT
        CASE
            WHEN vol_at_decision < 0.30 THEN '<0.30 (calm)'
            WHEN vol_at_decision < 0.60 THEN '0.30-0.60 (normal)'
            WHEN vol_at_decision < 1.00 THEN '0.60-1.00 (active)'
            ELSE '1.00+ (extreme)'
        END AS vol_bucket,
        CASE
            WHEN vol_at_decision < 0.30 THEN 1
            WHEN vol_at_decision < 0.60 THEN 2
            WHEN vol_at_decision < 1.00 THEN 3
            ELSE 4
        END AS bucket_order,
        COUNT(*)                            AS trades,
        COUNT(*) FILTER (WHERE won)         AS wins,
        COUNT(*) FILTER (WHERE NOT won)     AS losses,
        COALESCE(AVG(abs_edge), 0)          AS avg_edge,
        COALESCE(SUM(pnl), 0)               AS total_pnl,
        COALESCE(AVG(pnl), 0)               AS avg_pnl,
        COALESCE(SUM(size), 0)              AS total_staked,
        COALESCE(AVG(size / NULLIF(fill_price_assumed, 0)), 0) AS avg_contracts,
        AVG(brier)                          AS avg_brier
    FROM quant_short_v
    WHERE pnl IS NOT NULL
      AND (%(asset)s::text IS NULL OR asset_id = %(asset)s)
    GROUP BY vol_bucket, bucket_order
    ORDER BY bucket_order
"""


def _fmt_duration(seconds: int) -> str:
    """Format a duration in seconds to a human-readable string (e.g. 300 -> '5m')."""
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _render_breakdown(console: Console, rows, title: str, key_col: str, key_fmt) -> None:
    table = Table(title=title)
    table.add_column(key_col, style="cyan")
    table.add_column("Trades", justify="right")
    table.add_column("W/L", justify="right")
    table.add_column("Win%", justify="right")
    table.add_column("Avg |Edge|", justify="right")
    table.add_column("Staked", justify="right")
    table.add_column("Avg Ctrs", justify="right", style="dim")
    table.add_column("Brier", justify="right", style="dim")
    table.add_column("Avg P&L", justify="right")
    table.add_column("Total P&L", justify="right")
    table.add_column("ROI", justify="right")
    if not rows:
        table.add_row("(none)", "0", "-", "-", "-", "-", "-", "-", "-", "$0.00", "-")
    else:
        for r in rows:
            trades = int(r["trades"])
            wins = int(r["wins"])
            losses = int(r["losses"])
            win_pct = (wins / trades * 100) if trades else 0.0
            total_pnl = float(r["total_pnl"])
            avg_pnl = float(r["avg_pnl"])
            avg_edge = float(r["avg_edge"])
            total_staked = float(r["total_staked"])
            avg_contracts = float(r["avg_contracts"])
            avg_brier_raw = r.get("avg_brier") if hasattr(r, "get") else r["avg_brier"]
            avg_brier = float(avg_brier_raw) if avg_brier_raw is not None else None
            roi = (total_pnl / total_staked * 100) if total_staked > 0 else 0.0
            pnl_style = "green" if total_pnl >= 0 else "red"
            roi_style = "green" if roi >= 0 else "red"
            avg_pnl_style = "green" if avg_pnl >= 0 else "red"
            # Lower Brier is better. 0.25 = random, 0.0 = perfect.
            # Highlight when calibration is worse than coin-flip so it
            # doesn't blend into the dim column treatment.
            brier_str = "-" if avg_brier is None else f"{avg_brier:.3f}"
            brier_style = "red" if avg_brier is not None and avg_brier > 0.25 else "dim"
            table.add_row(
                key_fmt(r),
                str(trades),
                f"{wins}/{losses}",
                f"{win_pct:.1f}%",
                f"{avg_edge:.4f}",
                f"${total_staked:,.2f}",
                f"{avg_contracts:.2f}",
                f"[{brier_style}]{brier_str}[/{brier_style}]",
                f"[{avg_pnl_style}]${avg_pnl:+,.2f}[/{avg_pnl_style}]",
                f"[{pnl_style}]${total_pnl:+,.2f}[/{pnl_style}]",
                f"[{roi_style}]{roi:+.1f}%[/{roi_style}]",
            )
    console.print(table)


# Rejection breakdown: which gates are filtering markets out, and how
# often. Pairs naturally with the trade-level views above; together they
# give the full picture of what the decider considered, accepted, and
# refused. Defaults to the last 24 hours so the result tracks "current
# behaviour" rather than ancient cycles.
STATS_QUERY_REJECTIONS = """
    SELECT reason,
           COUNT(*)                 AS n,
           AVG(abs_edge)            AS avg_abs_edge,
           AVG(p_up)                AS avg_p_up,
           AVG(mid)                 AS avg_mid,
           AVG(vol)                 AS avg_vol
    FROM quant_decider_rejections
    WHERE decision_ts > NOW() - (%(hours)s * INTERVAL '1 hour')
    GROUP BY reason
    ORDER BY n DESC
"""


def _render_rejections(console: Console, rows, hours: float) -> None:
    table = Table(title=f"Decider Rejections (last {hours:g}h)")
    table.add_column("Reason", style="cyan")
    table.add_column("Count", justify="right")
    table.add_column("Avg |Edge|", justify="right", style="dim")
    table.add_column("Avg p_up", justify="right", style="dim")
    table.add_column("Avg Mid", justify="right", style="dim")
    table.add_column("Avg Vol", justify="right", style="dim")
    if not rows:
        table.add_row("(none)", "0", "-", "-", "-", "-")
        console.print(table)
        return
    total = sum(int(r["n"]) for r in rows)
    for r in rows:
        n = int(r["n"])
        share = (n / total * 100) if total else 0.0
        edge = r["avg_abs_edge"]
        p_up = r["avg_p_up"]
        mid = r["avg_mid"]
        vol = r["avg_vol"]
        table.add_row(
            r["reason"],
            f"{n} ({share:.0f}%)",
            f"{float(edge):.4f}" if edge is not None else "-",
            f"{float(p_up):.3f}" if p_up is not None else "-",
            f"{float(mid):.3f}" if mid is not None else "-",
            f"{float(vol):.3f}" if vol is not None else "-",
        )
    console.print(table)


@click.command("quant-stats")
@click.option("--asset", type=str, default=None,
              help="Filter to a single asset_id (e.g. BTC). Default: all assets.")
@click.option("--by-duration", is_flag=True,
              help="Break out performance per window duration (5m vs 15m vs ...).")
@click.option("--by-asset", is_flag=True,
              help="Break out performance per asset_id.")
@click.option("--by-edge", is_flag=True,
              help="Calibration: bucket trades by |edge| and show win% per bucket. "
                   "Win% should rise with edge magnitude if the model has signal.")
@click.option("--by-side", is_flag=True,
              help="Side bias: YES vs NO win rates. Persistent skew suggests "
                   "routing or pricing asymmetry, not random variance.")
@click.option("--by-vol", is_flag=True,
              help="Bucket by realized vol at decision. Helps spot regimes "
                   "where the lognormal estimator is over- or under-confident.")
@click.option("--rejections", is_flag=True,
              help="Show why the decider has been filtering markets out: "
                   "gate-by-gate breakdown of recent rejections.")
@click.option("--hours", type=float, default=24.0,
              help="Lookback window for --rejections (default 24h).")
def quant_stats(
    asset: str | None,
    by_duration: bool,
    by_asset: bool,
    by_edge: bool,
    by_side: bool,
    by_vol: bool,
    rejections: bool,
    hours: float,
) -> None:
    """Paper-trading performance of the quant short-horizon subsystem."""
    console = Console()
    settings = Settings.from_env()
    db = Database(settings)

    try:
        if rejections:
            with db.cursor() as cur:
                cur.execute(STATS_QUERY_REJECTIONS, {"hours": float(hours)})
                rows = cur.fetchall()
            _render_rejections(console, rows, hours)
            return

        if by_asset:
            with db.cursor() as cur:
                cur.execute(STATS_QUERY_BY_ASSET)
                rows = cur.fetchall()
            _render_breakdown(
                console, rows,
                title="Quant Up/Down Performance by Asset",
                key_col="Asset",
                key_fmt=lambda r: r["asset_id"],
            )
            return

        if by_duration:
            with db.cursor() as cur:
                cur.execute(STATS_QUERY_BY_DURATION, {"asset": asset})
                rows = cur.fetchall()
            title = (
                f"Quant Up/Down Performance by Timeframe ({asset})"
                if asset else "Quant Up/Down Performance by Timeframe"
            )
            _render_breakdown(
                console, rows,
                title=title,
                key_col="Window",
                key_fmt=lambda r: _fmt_duration(int(r["window_duration_s"])),
            )
            return

        if by_edge:
            with db.cursor() as cur:
                cur.execute(STATS_QUERY_BY_EDGE, {"asset": asset})
                rows = cur.fetchall()
            title = (
                f"Quant Up/Down Calibration by |Edge| ({asset})"
                if asset else "Quant Up/Down Calibration by |Edge|"
            )
            _render_breakdown(
                console, rows,
                title=title,
                key_col="|Edge|",
                key_fmt=lambda r: r["edge_bucket"],
            )
            return

        if by_side:
            with db.cursor() as cur:
                cur.execute(STATS_QUERY_BY_SIDE, {"asset": asset})
                rows = cur.fetchall()
            title = (
                f"Quant Up/Down Performance by Side ({asset})"
                if asset else "Quant Up/Down Performance by Side"
            )
            _render_breakdown(
                console, rows,
                title=title,
                key_col="Side",
                key_fmt=lambda r: r["side"],
            )
            return

        if by_vol:
            with db.cursor() as cur:
                cur.execute(STATS_QUERY_BY_VOL, {"asset": asset})
                rows = cur.fetchall()
            title = (
                f"Quant Up/Down Performance by Vol Regime ({asset})"
                if asset else "Quant Up/Down Performance by Vol Regime"
            )
            _render_breakdown(
                console, rows,
                title=title,
                key_col="Vol",
                key_fmt=lambda r: r["vol_bucket"],
            )
            return

        with db.cursor() as cur:
            cur.execute(STATS_QUERY_TOTAL, {"asset": asset})
            row = cur.fetchone()

        trades = int(row["trades"] or 0)
        wins = int(row["wins"] or 0)
        losses = int(row["losses"] or 0)
        avg_edge = float(row["avg_edge"] or 0)
        total_pnl = float(row["total_pnl"] or 0)
        avg_pnl = float(row["avg_pnl"] or 0)
        avg_vol = float(row["avg_vol"] or 0)
        total_staked = float(row["total_staked"] or 0)
        avg_size = float(row["avg_size"] or 0)
        avg_contracts = float(row["avg_contracts"] or 0)
        avg_brier_raw = row["avg_brier"]
        avg_brier = float(avg_brier_raw) if avg_brier_raw is not None else None
        roi = (total_pnl / total_staked * 100) if total_staked > 0 else 0

        title = (
            f"Quant Up/Down Paper-Trading Performance ({asset})"
            if asset else "Quant Up/Down Paper-Trading Performance"
        )
        table = Table(title=title)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right")

        if trades == 0:
            table.add_row("Trades", "0")
            console.print(table)
            return

        win_pct = (wins / trades) * 100
        pnl_style = "green" if total_pnl >= 0 else "red"
        table.add_row("Trades", str(trades))
        table.add_row("W/L", f"{wins}/{losses}")
        table.add_row("Win%", f"{win_pct:.1f}%")
        table.add_row("Avg |Edge|", f"{avg_edge:.4f}")
        table.add_row("Total Staked", f"${total_staked:,.2f}")
        table.add_row("Avg Size", f"${avg_size:.2f}")
        table.add_row("Avg Contracts", f"{avg_contracts:.2f}")
        table.add_row("Avg P&L", f"${avg_pnl:+,.2f}")
        table.add_row("Total P&L", f"[{pnl_style}]${total_pnl:+,.2f}[/{pnl_style}]")
        table.add_row("ROI", f"{roi:+.2f}%")
        table.add_row("Avg Realized Vol", f"{avg_vol:.3f}")
        if avg_brier is not None:
            brier_style = "red" if avg_brier > 0.25 else "green"
            table.add_row(
                "Avg Brier",
                f"[{brier_style}]{avg_brier:.3f}[/{brier_style}]"
                " (lower=better, 0.25=random)",
            )

        console.print(table)
    finally:
        db.close()

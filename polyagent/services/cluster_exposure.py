"""Pure helpers for computing aggregate exposure within a correlation cluster.

A *cluster* groups positions that resolve on the same settlement event
(today, that means same crypto asset and same resolution date). The
helper here filters open-position rows down to a target cluster and sums
their ``position_size`` values, leaving I/O and orchestration to the
caller.

Used by :class:`polyagent.data.repositories.positions.PositionRepository`
to back the executor's correlation cap.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Mapping

from polyagent.services.quant.strike.parser import cluster_key


def sum_positions_in_cluster(
    positions: Iterable[Mapping],
    target: tuple[str, str],
) -> Decimal:
    """Return the summed ``position_size`` of rows whose question maps to ``target``.

    Args:
        positions: Iterable of mapping-like rows. Each row must expose
            ``question`` (str) and ``position_size`` (Decimal-coercible).
        target: ``(asset_id, resolution_date_lower)`` tuple as produced
            by :func:`cluster_key`.

    Rows whose ``question`` is not a recognised strike pattern, or which
    parse to a different cluster, contribute zero. Missing or null
    ``position_size`` values are skipped silently.
    """
    total = Decimal("0")
    for row in positions:
        question = row.get("question") or ""
        if cluster_key(question) != target:
            continue
        size = row.get("position_size")
        if size is None:
            continue
        total += Decimal(str(size))
    return total

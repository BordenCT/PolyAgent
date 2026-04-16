"""Inter-thread queue definitions."""
from __future__ import annotations

from queue import Queue
from dataclasses import dataclass, field
from uuid import UUID

from polyagent.models import MarketData, Score, Thesis


@dataclass
class ScanResult:
    """Output of the scanner: a market that survived filtering."""
    market: MarketData
    market_db_id: UUID
    score: Score


@dataclass
class Queues:
    """All inter-thread queues for the pipeline."""
    scan_queue: Queue[ScanResult] = field(default_factory=Queue)
    thesis_queue: Queue[Thesis] = field(default_factory=Queue)
    shutdown: Queue[bool] = field(default_factory=Queue)

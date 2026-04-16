"""Strategy protocol — all strategies implement evaluate() -> Vote."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from polyagent.models import Vote


@runtime_checkable
class Strategy(Protocol):
    """Interface for a trading strategy agent."""

    @property
    def name(self) -> str: ...

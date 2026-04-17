"""Dynamic worker pool that auto-scales to available CPU cores."""
from __future__ import annotations

import logging
import os
import threading
from typing import Callable

logger = logging.getLogger("polyagent.infra.pool")


class WorkerPool:
    """Manages worker threads with auto-scaling based on cpu_count."""

    def __init__(self) -> None:
        self._threads: list[threading.Thread] = []
        self._cpu_count = os.cpu_count() or 4

    def compute_workers(
        self,
        component: str,
        divisor: int,
        override: int | None = None,
    ) -> int:
        """Compute worker count for a component.

        Args:
            component: Name for logging.
            divisor: cpu_count // divisor = default workers.
            override: Explicit worker count from env var.
        """
        if override is not None:
            count = override
        else:
            count = max(1, self._cpu_count // divisor)
        logger.info("%s: %d workers (cpus=%d)", component, count, self._cpu_count)
        return count

    def spawn(
        self,
        name: str,
        target: Callable,
        count: int,
        daemon: bool = True,
    ) -> list[threading.Thread]:
        """Spawn `count` worker threads running `target`."""
        threads = []
        for i in range(count):
            t = threading.Thread(
                target=target,
                name=f"{name}-{i}",
                daemon=daemon,
            )
            t.start()
            threads.append(t)
            self._threads.append(t)
        logger.info("Spawned %d %s workers", count, name)
        return threads

    def join_all(self, timeout: float = 30.0) -> None:
        """Wait for all threads to finish."""
        for t in self._threads:
            t.join(timeout=timeout)

    @property
    def active_count(self) -> int:
        return sum(1 for t in self._threads if t.is_alive())

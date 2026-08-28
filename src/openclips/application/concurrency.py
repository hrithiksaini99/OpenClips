"""Bounded per-stage concurrency limiting for worker handlers."""

from collections.abc import Iterator
from contextlib import contextmanager
from threading import BoundedSemaphore


class StageLimiter:
    """Caps concurrent executions of named pipeline stages via bounded semaphores.

    Stages absent from ``limits`` are treated as unbounded: :meth:`limit` becomes a
    pass-through context manager that never blocks.
    """

    def __init__(self, limits: dict[str, int]) -> None:
        self._semaphores: dict[str, BoundedSemaphore] = {
            stage: BoundedSemaphore(limit) for stage, limit in limits.items()
        }

    @contextmanager
    def limit(self, stage: str) -> Iterator[None]:
        """Hold a permit for ``stage`` for the duration of the ``with`` block."""
        semaphore = self._semaphores.get(stage)
        if semaphore is None:
            yield
            return
        semaphore.acquire()
        try:
            yield
        finally:
            semaphore.release()

"""Relay durable outbox intent to the at-least-once job queue."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from openclips.domain.outbox import outbox_backoff_seconds
from openclips.infrastructure.repositories import OutboxRepository


class JobQueue(Protocol):
    def enqueue(self, queue_name: str, payload: str) -> None: ...


class OutboxRelay:
    """Deliver one bounded batch while keeping PostgreSQL authoritative."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        queue: JobQueue,
        clock: Callable[[], datetime],
        batch_size: int,
        backoff_cap_seconds: int,
    ) -> None:
        self._session_factory = session_factory
        self._queue = queue
        self._clock = clock
        self._batch_size = batch_size
        self._backoff_cap_seconds = backoff_cap_seconds

    def dispatch_once(self) -> int:
        """Deliver due events, retaining failed events for a later retry."""
        now = self._clock()
        delivered = 0
        with self._session_factory() as session:
            events = OutboxRepository(session)
            for event in events.due(now, self._batch_size):
                try:
                    self._queue.enqueue(event.queue_name, str(event.job_id))
                except Exception as error:
                    attempts = event.attempts + 1
                    delay = outbox_backoff_seconds(attempts, self._backoff_cap_seconds)
                    events.mark_failed(
                        event.id,
                        _error_message(error),
                        now + timedelta(seconds=delay),
                    )
                else:
                    events.mark_delivered(event.id, now)
                    delivered += 1
            session.commit()
        return delivered


def _error_message(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:1000]

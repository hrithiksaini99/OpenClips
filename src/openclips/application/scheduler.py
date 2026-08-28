"""Poll PostgreSQL for due publications and hand them to the durable outbox."""

import logging
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from openclips.application.publishing import ScheduleCoordinator
from openclips.infrastructure.repositories import (
    ClipRepository,
    JobRepository,
    PublicationRepository,
)

logger = logging.getLogger(__name__)


class PublicationScheduler:
    """Turn due publications into queued jobs, one bounded batch per poll.

    Like ``OutboxRelay`` this owns the transaction: the claim, the ``ENQUEUE``
    transitions and the outbox rows commit together, so a crash mid-poll leaves
    the publications ``SCHEDULED`` and the next poll simply claims them again.
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        clock: Callable[[], datetime],
        poll_interval_seconds: float,
        limit: int = 50,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self.poll_interval_seconds = poll_interval_seconds
        self._limit = limit

    def dispatch_once(self) -> int:
        """Queue one batch of due publications; return how many jobs were created."""
        with self._session_factory() as session:
            coordinator = ScheduleCoordinator(
                clips=ClipRepository(session),
                publications=PublicationRepository(session),
                jobs=JobRepository(session),
                clock=self._clock,
            )
            jobs = coordinator.enqueue_due(limit=self._limit)
            session.commit()
        if jobs:
            logger.info("Queued %s due publications", len(jobs))
        return len(jobs)

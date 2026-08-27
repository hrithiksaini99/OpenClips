"""Durable PostgreSQL-to-queue outbox relay behavior."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openclips.application.dispatch import OutboxRelay
from openclips.domain.outbox import OutboxStatus
from openclips.infrastructure.models import Base, OutboxRecord
from openclips.infrastructure.queue import InMemoryJobQueue
from openclips.infrastructure.repositories import JobRepository
from openclips.worker import process_once


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_dispatch_once_enqueues_due_job_and_marks_event_delivered() -> None:
    session_factory = _session_factory()
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        job, event = JobRepository(session).create_dispatched(
            "transcribe", payload="source-id", queue_name="default"
        )
        job_id = job.id
        event_id = event.id
        event.available_at = now
        session.commit()

    queue = InMemoryJobQueue()
    delivered = OutboxRelay(
        session_factory=session_factory,
        queue=queue,
        clock=lambda: now,
        batch_size=10,
        backoff_cap_seconds=300,
    ).dispatch_once()

    assert delivered == 1
    receipt = queue.claim("default", timeout_seconds=0)
    assert receipt is not None
    assert receipt.payload == str(job_id)
    with session_factory() as session:
        refreshed = session.get(OutboxRecord, event_id)
        assert refreshed is not None
        assert refreshed.status is OutboxStatus.DELIVERED
        assert refreshed.delivered_at == now.replace(tzinfo=None)


def test_dispatch_once_records_failure_for_only_the_failed_event() -> None:
    session_factory = _session_factory()
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        good_job, good_event = JobRepository(session).create_dispatched(
            "transcribe", payload="good", queue_name="default"
        )
        bad_job, bad_event = JobRepository(session).create_dispatched(
            "transcribe", payload="bad", queue_name="default"
        )
        good_job_id = good_job.id
        bad_job_id = bad_job.id
        good_event_id = good_event.id
        bad_event_id = bad_event.id
        good_event.available_at = now
        bad_event.available_at = now
        session.commit()

    queue = _FailingPayloadQueue(failing_job_id=bad_job_id)
    delivered = OutboxRelay(
        session_factory=session_factory,
        queue=queue,
        clock=lambda: now,
        batch_size=10,
        backoff_cap_seconds=300,
    ).dispatch_once()

    assert delivered == 1
    receipt = queue.claim("default", timeout_seconds=0)
    assert receipt is not None
    assert receipt.payload == str(good_job_id)
    with session_factory() as session:
        refreshed_good = session.get(OutboxRecord, good_event_id)
        refreshed_bad = session.get(OutboxRecord, bad_event_id)
        assert refreshed_good is not None
        assert refreshed_good.status is OutboxStatus.DELIVERED
        assert refreshed_bad is not None
        assert refreshed_bad.status is OutboxStatus.PENDING
        assert refreshed_bad.attempts == 1
        assert refreshed_bad.last_error == "ConnectionError: Redis unavailable"
        assert refreshed_bad.available_at == (now + timedelta(seconds=1)).replace(tzinfo=None)


class _FailingPayloadQueue(InMemoryJobQueue):
    def __init__(self, failing_job_id: UUID) -> None:
        super().__init__()
        self._failing_job_id = failing_job_id

    def enqueue(self, queue_name: str, payload: str) -> None:
        if payload == str(self._failing_job_id):
            raise ConnectionError("Redis unavailable")
        super().enqueue(queue_name, payload)


def test_worker_acknowledges_unknown_job_after_durable_ignore() -> None:
    queue = InMemoryJobQueue()
    queue.enqueue("default", str(uuid4()))

    handled = process_once(
        session_factory=_session_factory(),
        handlers={},
        queue=queue,
        claim_timeout_seconds=0,
    )

    assert handled is True
    assert queue.processing_depth("default") == 0


def test_worker_leaves_receipt_for_recovery_when_payload_cannot_be_processed() -> None:
    queue = InMemoryJobQueue()
    queue.enqueue("default", "not-a-uuid")

    with pytest.raises(ValueError):
        process_once(
            session_factory=_session_factory(),
            handlers={},
            queue=queue,
            claim_timeout_seconds=0,
        )

    assert queue.processing_depth("default") == 1

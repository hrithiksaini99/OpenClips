"""Real-Redis delivery: outbox relay to reliable ready/processing claims."""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from openclips.application.dispatch import OutboxRelay
from openclips.domain.outbox import OutboxStatus
from openclips.infrastructure.models import OutboxRecord
from openclips.infrastructure.queue import QueueReceipt, RedisJobQueue
from openclips.infrastructure.repositories import JobRepository

pytestmark = pytest.mark.integration

QUEUE = "default"


def _redis_client():  # type: ignore[no-untyped-def]
    url = os.getenv("REDIS_URL")
    if not url:
        pytest.skip("REDIS_URL is not configured")
    import redis

    client = redis.Redis.from_url(url)
    if client.connection_pool.connection_kwargs.get("db") != 15:
        raise pytest.UsageError("REDIS_URL must target the reserved Redis database 15")
    client.flushdb()
    return client


def test_relay_delivers_due_job_to_redis_and_marks_delivered(
    session_factory: sessionmaker[Session],
) -> None:
    client = _redis_client()
    queue = RedisJobQueue(client)
    with session_factory() as session:
        job, event = JobRepository(session).create_dispatched(
            "transcribe", payload=str(uuid4()), queue_name=QUEUE
        )
        event_id = event.id
        job_id = job.id
        session.commit()

    relay = OutboxRelay(
        session_factory=session_factory,
        queue=queue,
        clock=lambda: datetime.now(UTC),
        batch_size=50,
        backoff_cap_seconds=300,
    )
    delivered = relay.dispatch_once()

    assert delivered == 1
    assert queue.depth(QUEUE) == 1
    with session_factory() as session:
        stored = session.get(OutboxRecord, event_id)
        assert stored is not None
        assert stored.status is OutboxStatus.DELIVERED
    receipt = queue.claim(QUEUE, timeout_seconds=1)
    assert receipt is not None
    assert receipt.payload == str(job_id)
    assert queue.processing_depth(QUEUE) == 1


def test_restore_processing_redelivers_unacked_claim() -> None:
    client = _redis_client()
    queue = RedisJobQueue(client)
    queue.enqueue(QUEUE, "job-1")
    claimed = queue.claim(QUEUE, timeout_seconds=1)

    assert claimed is not None
    assert queue.processing_depth(QUEUE) == 1
    restored = queue.restore_processing(QUEUE)

    assert restored == 1
    assert queue.processing_depth(QUEUE) == 0
    again = queue.claim(QUEUE, timeout_seconds=1)
    assert again is not None and again.payload == "job-1"


def test_restore_processing_preserves_fifo_priority_on_real_redis() -> None:
    client = _redis_client()
    queue = RedisJobQueue(client)

    queue.enqueue(QUEUE, "job-1")
    queue.enqueue(QUEUE, "job-2")
    queue.claim(QUEUE, timeout_seconds=1)
    queue.claim(QUEUE, timeout_seconds=1)
    queue.enqueue(QUEUE, "job-3")
    queue.restore_processing(QUEUE)

    assert [
        queue.claim(QUEUE, timeout_seconds=1),
        queue.claim(QUEUE, timeout_seconds=1),
        queue.claim(QUEUE, timeout_seconds=1),
    ] == [
        QueueReceipt(QUEUE, "job-1"),
        QueueReceipt(QUEUE, "job-2"),
        QueueReceipt(QUEUE, "job-3"),
    ]

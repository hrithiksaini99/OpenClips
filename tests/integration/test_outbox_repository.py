from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from openclips.domain.outbox import OutboxStatus
from openclips.domain.sources import SourceKind
from openclips.infrastructure.models import SourceAssetRecord
from openclips.infrastructure.repositories import JobRepository, OutboxRepository

pytestmark = pytest.mark.integration


def test_create_dispatched_flushes_job_and_pending_outbox(session: Session) -> None:
    job, event = JobRepository(session).create_dispatched(
        "transcribe", payload=str(uuid4()), queue_name="default"
    )

    assert event.job_id == job.id
    assert event.queue_name == "default"
    assert event.status is OutboxStatus.PENDING
    assert event.attempts == 0
    assert event.available_at is not None


def test_due_returns_only_pending_events_available_at_or_before_now(session: Session) -> None:
    jobs = JobRepository(session)
    events = OutboxRepository(session)
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    _, due_event = jobs.create_dispatched("transcribe", payload="due", queue_name="default")
    _, future_event = jobs.create_dispatched("transcribe", payload="future", queue_name="default")
    _, delivered_event = jobs.create_dispatched(
        "transcribe", payload="delivered", queue_name="default"
    )
    due_event.available_at = now - timedelta(seconds=1)
    future_event.available_at = now + timedelta(seconds=1)
    events.mark_delivered(delivered_event.id, now)

    due = events.due(now, limit=10)

    assert [event.id for event in due] == [due_event.id]


def test_mark_delivered_records_delivery_timestamp(session: Session) -> None:
    _, event = JobRepository(session).create_dispatched(
        "transcribe", payload="source-id", queue_name="default"
    )
    delivered_at = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)

    delivered = OutboxRepository(session).mark_delivered(event.id, delivered_at)

    assert delivered.status is OutboxStatus.DELIVERED
    assert delivered.delivered_at == delivered_at


def test_mark_failed_preserves_pending_event_for_later_retry(session: Session) -> None:
    _, event = JobRepository(session).create_dispatched(
        "transcribe", payload="source-id", queue_name="default"
    )
    next_attempt_at = datetime(2026, 8, 27, 12, 1, tzinfo=UTC)

    failed = OutboxRepository(session).mark_failed(
        event.id, "Redis unavailable", next_attempt_at
    )

    assert failed.status is OutboxStatus.PENDING
    assert failed.attempts == 1
    assert failed.last_error == "Redis unavailable"
    assert failed.available_at == next_attempt_at


def test_source_asset_auto_process_defaults_to_true(session: Session) -> None:
    source = SourceAssetRecord(
        source_kind=SourceKind.LOCAL_UPLOAD,
        original_locator="clip.mp4",
        idempotency_key=uuid4().hex,
        display_name="clip.mp4",
        retain_until=datetime.now(UTC) + timedelta(days=7),
    )
    session.add(source)
    session.flush()

    assert source.auto_process is True

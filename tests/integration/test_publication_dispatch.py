"""PostgreSQL contracts for atomic, idempotent publication dispatch."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from openclips.application.publishing import ScheduleCoordinator
from openclips.application.scheduler import PublicationScheduler
from openclips.domain.clips import ClipEvent
from openclips.domain.outbox import OutboxStatus
from openclips.domain.publishing import Platform, PublicationStatus
from openclips.domain.sources import SourceEvent, SourceKind
from openclips.infrastructure.media_storage import MediaStorage
from openclips.infrastructure.models import JobRecord, OutboxRecord, PublicationRecord
from openclips.infrastructure.queue import InMemoryJobQueue
from openclips.infrastructure.repositories import (
    ClipRepository,
    JobRepository,
    PublicationRepository,
    SourceRepository,
)
from openclips.providers.platforms.base import PublishResult
from openclips.worker import Handler, process_once

pytestmark = pytest.mark.integration

PLATFORM = Platform.YOUTUBE_SHORTS
FROZEN_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def _clock() -> datetime:
    return FROZEN_NOW


class CountingPublisher:
    """Records every platform call so redelivery can be proven side-effect free."""

    def __init__(self) -> None:
        self.calls = 0

    def publish(self, request: object) -> PublishResult:
        del request
        self.calls += 1
        return PublishResult(external_id="ext-1", external_url="https://x/ext-1")


def _seed_due_publication(
    session: Session, storage: MediaStorage, *, key: str
) -> PublicationRecord:
    """Create an approved, rendered clip with one publication already due."""
    sources = SourceRepository(session)
    source = sources.create(
        source_kind=SourceKind.LOCAL_UPLOAD,
        original_locator=f"{key}.mp4",
        external_id=None,
        idempotency_key=key,
        display_name=f"{key}.mp4",
        retain_until=FROZEN_NOW + timedelta(days=7),
    )
    sources.transition(source.id, SourceEvent.START)
    ready = sources.attach_media(source.id, media_path=f"local/{key}.mp4", byte_size=1)

    clips = ClipRepository(session)
    clip = clips.create(
        source_asset_id=ready.id, title=key, start_time=0.0, end_time=30.0
    )
    clips.transition(clip.id, ClipEvent.READY)
    clips.transition(clip.id, ClipEvent.APPROVE)
    clip.output_path = f"clips/{clip.id}/render.mp4"
    rendered = storage.resolve(clip.output_path)
    rendered.parent.mkdir(parents=True, exist_ok=True)
    rendered.write_bytes(b"x")

    record = PublicationRepository(session).create(
        clip_id=clip.id,
        platform=PLATFORM,
        scheduled_at=FROZEN_NOW - timedelta(minutes=1),
    )
    clips.transition(clip.id, ClipEvent.SCHEDULE)
    return record


def _coordinator(
    session: Session, storage: MediaStorage, publisher: CountingPublisher | None = None
) -> ScheduleCoordinator:
    return ScheduleCoordinator(
        clips=ClipRepository(session),
        publications=PublicationRepository(session),
        jobs=JobRepository(session),
        publishers={PLATFORM: publisher} if publisher is not None else {},
        storage=storage,
        clock=_clock,
    )


def _publish_handler(storage: MediaStorage, publisher: CountingPublisher) -> Handler:
    def handle(session: Session, job: JobRecord) -> None:
        _coordinator(session, storage, publisher).run(job)

    return handle


def test_concurrent_claim_due_never_hands_a_publication_to_two_workers(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    storage = MediaStorage(tmp_path)
    with session_factory() as setup:
        seeded = {
            _seed_due_publication(setup, storage, key=f"claim-{index}").id
            for index in range(2)
        }
        setup.commit()

    with session_factory() as first, session_factory() as second:
        first_claim = {
            record.id for record in PublicationRepository(first).claim_due(FROZEN_NOW, 10)
        }
        second_claim = {
            record.id for record in PublicationRepository(second).claim_due(FROZEN_NOW, 10)
        }
        first.rollback()
        second.rollback()

    assert first_claim.isdisjoint(second_claim)
    assert first_claim | second_claim == seeded


def test_repeated_enqueue_due_creates_one_job_and_outbox_row_per_publication(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    storage = MediaStorage(tmp_path)
    with session_factory() as setup:
        seeded = {
            _seed_due_publication(setup, storage, key=f"enqueue-{index}").id
            for index in range(2)
        }
        setup.commit()

    for _ in range(2):
        with session_factory() as session:
            _coordinator(session, storage).enqueue_due()
            session.commit()

    with session_factory() as session:
        jobs = session.query(JobRecord).filter(JobRecord.kind == PLATFORM.job_kind).all()
        assert {UUID(job.payload or "") for job in jobs} == seeded
        events = session.query(OutboxRecord).all()
        assert {event.job_id for event in events} == {job.id for job in jobs}
        assert [event.status for event in events] == [OutboxStatus.PENDING] * 2
        assert [event.queue_name for event in events] == [PLATFORM.job_kind] * 2
        records = session.query(PublicationRecord).all()
        assert {record.status for record in records} == {PublicationStatus.QUEUED}


def test_redelivered_publish_message_is_acknowledged_and_changes_nothing(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    storage = MediaStorage(tmp_path)
    publisher = CountingPublisher()
    with session_factory() as setup:
        _seed_due_publication(setup, storage, key="redelivery")
        setup.commit()
    with session_factory() as session:
        created = _coordinator(session, storage).enqueue_due()
        job_id = created[0].id
        publication_id = UUID(created[0].payload or "")
        session.commit()

    queue = InMemoryJobQueue()
    handlers = {PLATFORM.job_kind: _publish_handler(storage, publisher)}

    def _deliver() -> bool:
        queue.enqueue(PLATFORM.queue_name, str(job_id))
        return process_once(
            session_factory=session_factory,
            handlers=handlers,
            queue=queue,
            queue_names=(PLATFORM.queue_name,),
            claim_timeout_seconds=0.0,
        )

    assert _deliver() is True
    with session_factory() as session:
        published = PublicationRepository(session).get(publication_id)
        assert published is not None
        assert published.status is PublicationStatus.PUBLISHED
        snapshot = (
            published.status,
            published.attempts,
            published.external_id,
            published.published_at,
        )

    assert _deliver() is True

    with session_factory() as session:
        again = PublicationRepository(session).get(publication_id)
        assert again is not None
        assert (
            again.status,
            again.attempts,
            again.external_id,
            again.published_at,
        ) == snapshot
    assert publisher.calls == 1
    assert queue.processing_depth(PLATFORM.queue_name) == 0


def test_scheduler_dispatch_once_commits_one_batch_and_is_idempotent(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    storage = MediaStorage(tmp_path)
    with session_factory() as setup:
        seeded = {
            _seed_due_publication(setup, storage, key=f"scheduler-{index}").id
            for index in range(2)
        }
        setup.commit()
    scheduler = PublicationScheduler(
        session_factory=session_factory,
        clock=_clock,
        poll_interval_seconds=30.0,
        limit=1,
    )

    assert scheduler.dispatch_once() == 1
    assert scheduler.dispatch_once() == 1
    assert scheduler.dispatch_once() == 0

    with session_factory() as session:
        jobs = session.query(JobRecord).filter(JobRecord.kind == PLATFORM.job_kind).all()
        assert {UUID(job.payload or "") for job in jobs} == seeded
        assert session.query(OutboxRecord).count() == 2

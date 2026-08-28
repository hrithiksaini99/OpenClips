"""Scheduling and bounded-retry flow tests over an in-memory SQLite database."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openclips.application.publishing import (
    ClipNotApprovedError,
    DailyWindowRule,
    ScheduleCoordinator,
    SchedulingExhaustedError,
)
from openclips.domain.clips import ClipEvent, ClipStatus
from openclips.domain.errors import InvalidTransitionError
from openclips.domain.publishing import (
    MAX_PUBLICATION_ATTEMPTS,
    Platform,
    PublicationEvent,
    PublicationStatus,
    backoff_seconds,
)
from openclips.domain.sources import SourceEvent, SourceKind
from openclips.infrastructure.media_storage import MediaStorage
from openclips.infrastructure.models import Base, OutboxRecord
from openclips.infrastructure.queue import InMemoryJobQueue
from openclips.infrastructure.repositories import (
    ClipRepository,
    JobRepository,
    PublicationRepository,
    SourceRepository,
)
from openclips.providers.platforms.base import PublishError, PublishResult

QUEUE_NAME = Platform.YOUTUBE_SHORTS.queue_name
FROZEN_NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)


def _clock() -> datetime:
    return FROZEN_NOW


class FakePublisher:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.calls: list[str] = []

    def publish(self, request: object) -> PublishResult:
        self.calls.append(str(request))  # type: ignore[attr-defined]
        if len(self.calls) <= self.fail_times:
            raise PublishError("platform unavailable")
        return PublishResult(external_id="ext-1", external_url="https://x/ext-1")


@dataclass(frozen=True)
class _Harness:
    clips: ClipRepository
    publications: PublicationRepository
    jobs: JobRepository
    coordinator: ScheduleCoordinator
    publisher: FakePublisher
    session: Session


@pytest.fixture
def harness() -> Iterator[_Harness]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        sources = SourceRepository(session)
        record = sources.create(
            source_kind=SourceKind.LOCAL_UPLOAD,
            original_locator="show.mp4",
            external_id=None,
            idempotency_key="publish-key",
            display_name="show.mp4",
            retain_until=datetime.now(UTC) + timedelta(days=7),
        )
        sources.transition(record.id, SourceEvent.START)
        ready = sources.attach_media(record.id, media_path="local/show.mp4", byte_size=1)

        clips = ClipRepository(session)
        clip = clips.create(
            source_asset_id=ready.id,
            title="Approved clip",
            start_time=0.0,
            end_time=30.0,
        )
        clips.transition(clip.id, ClipEvent.READY)
        clips.transition(clip.id, ClipEvent.APPROVE)

        publisher = FakePublisher()
        publications = PublicationRepository(session)
        jobs = JobRepository(session)
        storage = MediaStorage(Path("/tmp/oc-publish-media"))
        (storage.root / "clips" / str(clip.id)).mkdir(parents=True, exist_ok=True)
        (storage.root / "clips" / str(clip.id) / "render.mp4").write_bytes(b"x")
        clip.output_path = f"clips/{clip.id}/render.mp4"
        session.flush()

        coordinator = ScheduleCoordinator(
            clips=clips,
            publications=publications,
            jobs=jobs,
            publishers={Platform.YOUTUBE_SHORTS: publisher},
            storage=storage,
            clock=_clock,
        )
        yield _Harness(
            clips=clips,
            publications=publications,
            jobs=jobs,
            coordinator=coordinator,
            publisher=publisher,
            session=session,
        )


def test_only_approved_clips_can_be_scheduled(harness: _Harness) -> None:
    draft = harness.clips.create(source_asset_id=None, title="Draft")
    harness.clips.transition(draft.id, ClipEvent.READY)

    with pytest.raises(ClipNotApprovedError):
        harness.coordinator.schedule(draft.id, Platform.YOUTUBE_SHORTS)


def test_schedule_transitions_clip_and_creates_record(harness: _Harness) -> None:
    clip_id = harness.clips.list_all()[0].id

    record = harness.coordinator.schedule(
        clip_id, Platform.INSTAGRAM_REELS, scheduled_at=FROZEN_NOW + timedelta(hours=2)
    )

    assert record.status is PublicationStatus.SCHEDULED
    assert record.platform is Platform.INSTAGRAM_REELS
    refreshed = harness.clips.get(clip_id)
    assert refreshed is not None
    assert refreshed.status is ClipStatus.SCHEDULED


def test_rule_based_slots_are_deterministic_and_ordered(harness: _Harness) -> None:
    rule = DailyWindowRule(times=(time(10, 0), time(18, 0)))
    first = rule.next_slot(FROZEN_NOW)

    assert first == datetime(2026, 8, 26, 18, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone"):
        rule.next_slot(datetime(2026, 8, 26, 12, 0))

    clip_id = harness.clips.list_all()[0].id
    records = harness.coordinator.schedule_by_rule([clip_id], Platform.YOUTUBE_SHORTS, rule)

    assert records[0].scheduled_at == first


def test_publish_success_records_external_identity(harness: _Harness) -> None:
    clip_id = harness.clips.list_all()[0].id
    record = harness.coordinator.schedule(clip_id, Platform.YOUTUBE_SHORTS)
    harness.coordinator.enqueue_due()

    published = harness.coordinator.publish_publication(record.id)

    assert published.status is PublicationStatus.PUBLISHED
    assert published.external_id == "ext-1"
    assert published.published_at is not None
    refreshed = harness.clips.get(clip_id)
    assert refreshed is not None
    assert refreshed.status is ClipStatus.PUBLISHED


def test_failure_preserves_reason_and_backs_off_within_budget(harness: _Harness) -> None:
    harness.publisher.fail_times = 2
    clip_id = harness.clips.list_all()[0].id
    record = harness.coordinator.schedule(clip_id, Platform.YOUTUBE_SHORTS)
    harness.coordinator.enqueue_due()

    after_first = harness.coordinator.publish_publication(record.id)
    assert after_first.status is PublicationStatus.SCHEDULED
    assert after_first.error is not None and "PublishError" in after_first.error
    assert after_first.attempts == 1
    assert after_first.scheduled_at == FROZEN_NOW + timedelta(seconds=backoff_seconds(1))

    harness.publisher.fail_times = 0
    harness.publications.transition(record.id, PublicationEvent.ENQUEUE)
    recovered = harness.coordinator.publish_publication(record.id)
    assert recovered.status is PublicationStatus.PUBLISHED
    assert recovered.attempts == 2


def test_retry_budget_is_bounded_and_observable(harness: _Harness) -> None:
    harness.publisher.fail_times = 99
    clip_id = harness.clips.list_all()[0].id
    record = harness.coordinator.schedule(clip_id, Platform.YOUTUBE_SHORTS)

    for _ in range(MAX_PUBLICATION_ATTEMPTS):
        harness.publications.transition(record.id, PublicationEvent.ENQUEUE)
        harness.coordinator.publish_publication(record.id)

    exhausted = harness.publications.get(record.id)
    assert exhausted is not None
    assert exhausted.attempts == MAX_PUBLICATION_ATTEMPTS
    assert exhausted.status is PublicationStatus.FAILED

    with pytest.raises(SchedulingExhaustedError):
        harness.coordinator.retry(record.id)


def test_enqueue_due_queues_each_due_publication_exactly_once(harness: _Harness) -> None:
    clip_id = harness.clips.list_all()[0].id
    due_record = harness.coordinator.schedule(
        clip_id, Platform.YOUTUBE_SHORTS, scheduled_at=FROZEN_NOW - timedelta(minutes=5)
    )

    created = harness.coordinator.enqueue_due()

    assert [job.kind for job in created] == ["publish.youtube_shorts"]
    assert [job.payload for job in created] == [str(due_record.id)]
    event = harness.session.query(OutboxRecord).filter_by(job_id=created[0].id).one()
    assert event.queue_name == "publish.youtube_shorts"
    queued = harness.publications.get(due_record.id)
    assert queued is not None and queued.status is PublicationStatus.QUEUED

    assert harness.coordinator.enqueue_due() == []
    assert harness.session.query(OutboxRecord).count() == 1


def test_publish_requires_a_queued_publication(harness: _Harness) -> None:
    clip_id = harness.clips.list_all()[0].id
    record = harness.coordinator.schedule(clip_id, Platform.YOUTUBE_SHORTS)

    with pytest.raises(InvalidTransitionError):
        harness.coordinator.publish_publication(record.id)

    assert harness.publisher.calls == []


def test_cancelled_publication_never_reaches_the_platform(harness: _Harness) -> None:
    clip_id = harness.clips.list_all()[0].id
    record = harness.coordinator.schedule(clip_id, Platform.YOUTUBE_SHORTS)
    harness.coordinator.enqueue_due()
    cancelled = harness.publications.transition(record.id, PublicationEvent.CANCEL)

    unchanged = harness.coordinator.publish_publication(record.id)

    assert unchanged.status is PublicationStatus.CANCELLED
    assert unchanged.attempts == cancelled.attempts == 0
    assert harness.publisher.calls == []


def test_worker_publishes_through_platform_queue(harness: _Harness) -> None:
    from openclips.worker import process_once

    factory = sessionmaker(bind=harness.session.get_bind())
    queue = InMemoryJobQueue()
    clip_id = harness.clips.list_all()[0].id
    record = harness.coordinator.schedule(
        clip_id, Platform.YOUTUBE_SHORTS, scheduled_at=FROZEN_NOW - timedelta(minutes=1)
    )
    harness.session.commit()
    jobs = harness.coordinator.enqueue_due()
    for job in jobs:
        queue.enqueue(job.kind, str(job.id))
    harness.session.commit()

    handled = process_once(
        session_factory=factory,
        handlers={
            "publish.youtube_shorts": _publish_handler(harness),
        },
        queue=queue,
        queue_names=(QUEUE_NAME,),
        claim_timeout_seconds=0.0,
    )

    assert handled is True
    harness.session.expire_all()
    refreshed = harness.publications.get(record.id)
    assert refreshed is not None
    assert refreshed.status is PublicationStatus.PUBLISHED
    clip = harness.clips.get(clip_id)
    assert clip is not None and clip.status is ClipStatus.PUBLISHED


def _publish_handler(harness: _Harness):  # type: ignore[no-untyped-def]
    def handle(session: Session, job: object) -> None:
        coordinator = ScheduleCoordinator(
            clips=ClipRepository(session),
            publications=PublicationRepository(session),
            jobs=JobRepository(session),
            publishers={Platform.YOUTUBE_SHORTS: harness.publisher},
            storage=MediaStorage(Path("/tmp/oc-publish-media")),
            clock=_clock,
        )
        coordinator.run(job)  # type: ignore[arg-type]

    return handle


def test_instagram_publish_fails_when_public_media_url_is_unavailable(harness: _Harness) -> None:
    from openclips.providers.media_urls import UnavailableMediaUrlProvider

    transport_calls: list[object] = []

    class RecordingPublisher:
        def publish(self, request: object) -> PublishResult:
            transport_calls.append(request)
            return PublishResult(external_id="x", external_url="https://x/x")

    coordinator = ScheduleCoordinator(
        clips=harness.clips,
        publications=harness.publications,
        jobs=harness.jobs,
        publishers={Platform.INSTAGRAM_REELS: RecordingPublisher()},
        storage=harness.coordinator.storage,
        clock=_clock,
        media_url_provider=UnavailableMediaUrlProvider(),
    )
    clip_id = harness.clips.list_all()[0].id
    record = coordinator.schedule(clip_id, Platform.INSTAGRAM_REELS)
    coordinator.enqueue_due()

    failed = coordinator.publish_publication(record.id)

    assert failed.status is PublicationStatus.SCHEDULED
    assert failed.error is not None and "OPENCLIPS_PUBLIC_MEDIA_BASE_URL" in failed.error
    assert transport_calls == []


def test_cancel_for_clip_cancels_scheduled_and_queued_only(harness: _Harness) -> None:
    clip_id = harness.clips.list_all()[0].id
    scheduled = harness.publications.create(
        clip_id=clip_id, platform=Platform.YOUTUBE_SHORTS, scheduled_at=FROZEN_NOW
    )
    queued = harness.publications.create(
        clip_id=clip_id, platform=Platform.INSTAGRAM_REELS, scheduled_at=FROZEN_NOW
    )
    harness.publications.transition(queued.id, PublicationEvent.ENQUEUE)
    published = harness.publications.create(
        clip_id=clip_id, platform=Platform.YOUTUBE_SHORTS, scheduled_at=FROZEN_NOW
    )
    harness.publications.transition(published.id, PublicationEvent.ENQUEUE)
    harness.publications.transition(published.id, PublicationEvent.START)
    harness.publications.attach_result(
        published.id, external_id="ext-1", external_url="https://x/ext-1"
    )

    count = harness.coordinator.cancel_for_clip(clip_id)

    assert count == 2
    assert harness.publications.get(scheduled.id).status is PublicationStatus.CANCELLED  # type: ignore[union-attr]
    assert harness.publications.get(queued.id).status is PublicationStatus.CANCELLED  # type: ignore[union-attr]
    assert harness.publications.get(published.id).status is PublicationStatus.PUBLISHED  # type: ignore[union-attr]


def test_backoff_is_exponential_and_capped() -> None:
    assert backoff_seconds(1) == 30
    assert backoff_seconds(2) == 60
    assert backoff_seconds(3) == 120
    assert backoff_seconds(20) == 3600
    with pytest.raises(ValueError, match="attempt"):
        backoff_seconds(0)

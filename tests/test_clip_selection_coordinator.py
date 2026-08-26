"""Clip selection coordinator flow tests over an in-memory SQLite database."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openclips.application.clipping import (
    SELECT_CLIPS_JOB_KIND,
    ClipSelectionCoordinator,
    TranscriptMissingError,
)
from openclips.domain.clips import ClipStatus
from openclips.domain.jobs import JobStatus
from openclips.domain.selection import ClipCandidate, SelectionBounds
from openclips.domain.sources import SourceEvent, SourceKind
from openclips.domain.transcripts import TranscriptDocument, TranscriptSegment, TranscriptWord
from openclips.infrastructure.models import Base, SourceAssetRecord
from openclips.infrastructure.queue import InMemoryJobQueue
from openclips.infrastructure.repositories import (
    ClipRepository,
    JobRepository,
    SourceRepository,
    TranscriptRepository,
)
from openclips.providers.llm import HeuristicClipRefiner, MalformedModelOutputError
from openclips.worker import make_select_clips_handler, process_once

QUEUE_NAME = "default"


def _word(text: str, start: float, end: float) -> TranscriptWord:
    return TranscriptWord(text=text, start=start, end=end, probability=0.95)


def _document() -> TranscriptDocument:
    texts = [
        "The biggest mistake everyone makes is never tracking their money.",
        "Let me show you one secret trick that always works.",
        "First write down every expense for thirty days.",
        "Then cut the two largest recurring charges.",
        "Automate savings on payday and never see the money.",
    ]
    segments = []
    for index, text in enumerate(texts):
        start = index * 25.0
        words = tuple(
            _word(token.strip(".,!?;:"), start + offset * 1.2, start + offset * 1.2 + 1.0)
            for offset, token in enumerate(text.split())
        )
        segments.append(
            TranscriptSegment(start=start, end=start + 24.0, text=text, words=words)
        )
    return TranscriptDocument(language="en", duration=125.0, segments=tuple(segments))


@dataclass(frozen=True)
class _Harness:
    sources: SourceRepository
    transcripts: TranscriptRepository
    clips: ClipRepository
    jobs: JobRepository
    session: Session


@pytest.fixture
def harness() -> Iterator[_Harness]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield _Harness(
            sources=SourceRepository(session),
            transcripts=TranscriptRepository(session),
            clips=ClipRepository(session),
            jobs=JobRepository(session),
            session=session,
        )


def _ready_source(harness: _Harness) -> SourceAssetRecord:
    record = harness.sources.create(
        source_kind=SourceKind.LOCAL_UPLOAD,
        original_locator="show.mp4",
        external_id=None,
        idempotency_key="selection-key",
        display_name="show.mp4",
        retain_until=datetime.now(UTC) + timedelta(days=7),
    )
    harness.sources.transition(record.id, SourceEvent.START)
    return harness.sources.attach_media(
        record.id, media_path="local_upload/show.mp4", byte_size=10
    )


def _coordinator(
    harness: _Harness, refiner: HeuristicClipRefiner | object
) -> ClipSelectionCoordinator:
    return ClipSelectionCoordinator(
        sources=harness.sources,
        transcripts=harness.transcripts,
        clips=harness.clips,
        jobs=harness.jobs,
        refiner=refiner,  # type: ignore[arg-type]
        bounds=SelectionBounds(max_clips=3, min_duration_seconds=20.0, max_duration_seconds=90.0),
    )


def test_selection_creates_reviewable_clips(harness: _Harness) -> None:
    source = _ready_source(harness)
    harness.transcripts.upsert_for_source(source.id, _document())
    harness.session.commit()
    coordinator = _coordinator(harness, HeuristicClipRefiner())

    job = coordinator.enqueue(source.id)
    records = coordinator.run(coordinator.jobs.get(job.id))  # type: ignore[arg-type]

    assert records
    stored = harness.clips.list_for_source(source.id)
    assert [record.id for record in stored] == [record.id for record in records]
    for record in stored:
        assert record.status is ClipStatus.READY_FOR_REVIEW
        assert record.title
        assert record.start_time is not None and record.end_time is not None
        assert record.end_time - record.start_time <= 90.0 + 1e-6


def test_selection_is_reproducible_and_replaces_previous_clips(harness: _Harness) -> None:
    source = _ready_source(harness)
    harness.transcripts.upsert_for_source(source.id, _document())
    harness.session.commit()
    coordinator = _coordinator(harness, HeuristicClipRefiner())

    first = coordinator.select_for_source(source.id)
    first_ids = [record.id for record in first]
    second = coordinator.select_for_source(source.id)

    assert [record.id for record in second] != first_ids
    assert len(harness.clips.list_for_source(source.id)) == len(first)


def test_enqueue_requires_transcribed_ready_source(harness: _Harness) -> None:
    source = harness.sources.create(
        source_kind=SourceKind.LOCAL_UPLOAD,
        original_locator="pending.mp4",
        external_id=None,
        idempotency_key="pending-selection",
        display_name="pending.mp4",
        retain_until=datetime.now(UTC) + timedelta(days=7),
    )
    coordinator = _coordinator(harness, HeuristicClipRefiner())

    with pytest.raises(TranscriptMissingError):
        coordinator.enqueue(source.id)

    ready = _ready_source(harness)
    with pytest.raises(TranscriptMissingError):
        coordinator.enqueue(ready.id)


def test_malformed_refiner_output_falls_back_to_heuristics(harness: _Harness) -> None:
    class ExplodingRefiner(HeuristicClipRefiner):
        def refine(self, candidates, document, bounds):  # type: ignore[no-untyped-def]
            msg = "model emitted garbage"
            raise MalformedModelOutputError(msg)

    source = _ready_source(harness)
    harness.transcripts.upsert_for_source(source.id, _document())
    harness.session.commit()
    coordinator = _coordinator(harness, ExplodingRefiner())

    records = coordinator.select_for_source(source.id)

    assert records
    assert all(record.status is ClipStatus.READY_FOR_REVIEW for record in records)


def test_worker_processes_selection_job_end_to_end(harness: _Harness) -> None:
    factory = sessionmaker(bind=harness.session.get_bind())
    queue = InMemoryJobQueue()
    source = _ready_source(harness)
    harness.transcripts.upsert_for_source(source.id, _document())
    harness.session.commit()
    coordinator = _coordinator(harness, HeuristicClipRefiner())
    job = coordinator.enqueue(source.id)
    queue.enqueue(QUEUE_NAME, str(job.id))

    handled = process_once(
        session_factory=factory,
        handlers={
            SELECT_CLIPS_JOB_KIND: make_select_clips_handler(
                HeuristicClipRefiner(), SelectionBounds()
            )
        },
        queue=queue,
        claim_timeout_seconds=0.0,
    )

    assert handled is True
    harness.session.expire_all()
    refreshed = harness.jobs.get(job.id)
    assert refreshed is not None
    assert refreshed.status is JobStatus.SUCCEEDED
    assert harness.clips.list_for_source(source.id)


def test_candidate_overlap_helper_detects_intersection() -> None:
    base = ClipCandidate(start=0.0, end=30.0, title="a", score=1.0, segment_indices=(0,))
    overlapping = ClipCandidate(start=20.0, end=50.0, title="b", score=1.0, segment_indices=(1,))
    disjoint = ClipCandidate(start=30.0, end=60.0, title="c", score=1.0, segment_indices=(2,))

    assert base.overlaps(overlapping) is True
    assert base.overlaps(disjoint) is False

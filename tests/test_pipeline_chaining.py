"""Automatic stage chaining: each successful stage dispatches its successor."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from openclips.application.clipping import SELECT_CLIPS_JOB_KIND, ClipSelectionCoordinator
from openclips.application.rendering import RENDER_CLIP_JOB_KIND
from openclips.application.transcription import TRANSCRIBE_JOB_KIND, TranscriptionCoordinator
from openclips.application.youtube_ingestion import (
    INGEST_YOUTUBE_JOB_KIND,
    YouTubeIngestionCoordinator,
)
from openclips.domain.outbox import OutboxStatus
from openclips.domain.selection import SelectionBounds
from openclips.domain.sources import SourceEvent, SourceKind, SourceStatus
from openclips.domain.transcripts import TranscriptDocument, TranscriptSegment, TranscriptWord
from openclips.infrastructure.media_storage import MediaStorage
from openclips.infrastructure.models import Base, OutboxRecord, SourceAssetRecord
from openclips.infrastructure.repositories import (
    ClipRepository,
    JobRepository,
    SourceRepository,
    TranscriptRepository,
)
from openclips.providers.llm import HeuristicClipRefiner
from openclips.providers.transcription import TranscriptionProvider
from openclips.providers.youtube import YtDlpDownloader

URL = "https://youtu.be/dQw4w9WgXcQ"


class _FixedProvider(TranscriptionProvider):
    def __init__(self, document: TranscriptDocument) -> None:
        self._document = document

    def is_ready(self) -> bool:
        return True

    def readiness(self) -> str:
        return "ready"

    def transcribe(self, media_path: Path) -> TranscriptDocument:
        del media_path
        return self._document


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
            TranscriptWord(
                text=token.strip(".,!?;:"),
                start=start + offset * 1.2,
                end=start + offset * 1.2 + 1.0,
                probability=0.95,
            )
            for offset, token in enumerate(text.split())
        )
        segments.append(TranscriptSegment(start=start, end=start + 24.0, text=text, words=words))
    return TranscriptDocument(language="en", duration=125.0, segments=tuple(segments))


@dataclass
class _Harness:
    session: Session
    sources: SourceRepository
    transcripts: TranscriptRepository
    clips: ClipRepository
    jobs: JobRepository
    storage: MediaStorage


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[_Harness]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield _Harness(
            session=session,
            sources=SourceRepository(session),
            transcripts=TranscriptRepository(session),
            clips=ClipRepository(session),
            jobs=JobRepository(session),
            storage=MediaStorage(tmp_path / "media"),
        )


def _ready_source(
    harness: _Harness, *, auto_process: bool, key: str = "chain"
) -> SourceAssetRecord:
    record = harness.sources.create(
        source_kind=SourceKind.LOCAL_UPLOAD,
        original_locator="show.mp4",
        external_id=None,
        idempotency_key=key,
        display_name="show.mp4",
        retain_until=datetime.now(UTC) + timedelta(days=7),
        auto_process=auto_process,
    )
    harness.sources.transition(record.id, SourceEvent.START)
    return harness.sources.attach_media(record.id, media_path="local_upload/show.mp4", byte_size=10)


def _transcription(harness: _Harness) -> TranscriptionCoordinator:
    return TranscriptionCoordinator(
        sources=harness.sources,
        transcripts=harness.transcripts,
        jobs=harness.jobs,
        provider=_FixedProvider(_document()),
        storage=harness.storage,
    )


def _selection(harness: _Harness) -> ClipSelectionCoordinator:
    return ClipSelectionCoordinator(
        sources=harness.sources,
        transcripts=harness.transcripts,
        clips=harness.clips,
        jobs=harness.jobs,
        refiner=HeuristicClipRefiner(),
        bounds=SelectionBounds(max_clips=3, min_duration_seconds=20.0, max_duration_seconds=90.0),
    )


def _pending_events(harness: _Harness, kind: str) -> list[OutboxRecord]:
    job_ids = [job.id for job in harness.jobs.list_all(kind=kind)]
    if not job_ids:
        return []
    return (
        harness.session.query(OutboxRecord)
        .filter(OutboxRecord.job_id.in_(job_ids))
        .all()
    )


def test_transcription_success_dispatches_selection_when_auto(harness: _Harness) -> None:
    source = _ready_source(harness, auto_process=True)
    job = _transcription(harness).enqueue(source.id)

    _transcription(harness).run(harness.jobs.get(job.id))  # type: ignore[arg-type]

    selection_jobs = harness.jobs.list_all(kind=SELECT_CLIPS_JOB_KIND)
    assert [job.payload for job in selection_jobs] == [str(source.id)]
    events = _pending_events(harness, SELECT_CLIPS_JOB_KIND)
    assert len(events) == 1
    assert events[0].status is OutboxStatus.PENDING


def test_transcription_success_skips_selection_when_manual(harness: _Harness) -> None:
    source = _ready_source(harness, auto_process=False)
    job = _transcription(harness).enqueue(source.id)

    _transcription(harness).run(harness.jobs.get(job.id))  # type: ignore[arg-type]

    assert harness.jobs.list_all(kind=SELECT_CLIPS_JOB_KIND) == []


def test_selection_dispatches_one_render_per_candidate(harness: _Harness) -> None:
    source = _ready_source(harness, auto_process=True)
    harness.transcripts.upsert_for_source(source.id, _document())

    clips = _selection(harness).select_for_source(source.id)

    assert clips
    render_jobs = harness.jobs.list_all(kind=RENDER_CLIP_JOB_KIND)
    assert sorted(job.payload for job in render_jobs) == sorted(str(clip.id) for clip in clips)
    assert len(_pending_events(harness, RENDER_CLIP_JOB_KIND)) == len(clips)


def test_selection_skips_render_when_manual(harness: _Harness) -> None:
    source = _ready_source(harness, auto_process=False)
    harness.transcripts.upsert_for_source(source.id, _document())

    clips = _selection(harness).select_for_source(source.id)

    assert clips
    assert harness.jobs.list_all(kind=RENDER_CLIP_JOB_KIND) == []


def test_selection_with_no_candidates_creates_no_render_jobs_but_succeeds(
    harness: _Harness,
) -> None:
    source = _ready_source(harness, auto_process=True)
    empty = TranscriptDocument(language="en", duration=1.0, segments=())
    harness.transcripts.upsert_for_source(source.id, empty)

    clips = _selection(harness).select_for_source(source.id)

    assert clips == []
    assert harness.jobs.list_all(kind=RENDER_CLIP_JOB_KIND) == []


def test_youtube_ingest_success_dispatches_transcribe_when_auto(harness: _Harness) -> None:
    def runner(argv: list[str]) -> object:
        Path(argv[argv.index("-o") + 1]).write_bytes(b"video-bytes")

        class _Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Completed()

    coordinator = YouTubeIngestionCoordinator(
        sources=harness.sources,
        jobs=harness.jobs,
        storage=harness.storage,
        downloader=YtDlpDownloader(runner=runner),  # type: ignore[arg-type]
    )
    _source, ingest_job = coordinator.register(URL, auto_process=True)

    ready = coordinator.run(ingest_job)

    assert ready.status is SourceStatus.READY
    transcribe_jobs = harness.jobs.list_all(kind=TRANSCRIBE_JOB_KIND)
    assert [job.payload for job in transcribe_jobs] == [str(ready.id)]
    # The ingest job itself is unrelated to the successor transcribe dispatch.
    assert harness.jobs.list_all(kind=INGEST_YOUTUBE_JOB_KIND)


def test_youtube_ingest_skips_transcribe_when_manual(harness: _Harness) -> None:
    def runner(argv: list[str]) -> object:
        Path(argv[argv.index("-o") + 1]).write_bytes(b"video-bytes")

        class _Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Completed()

    coordinator = YouTubeIngestionCoordinator(
        sources=harness.sources,
        jobs=harness.jobs,
        storage=harness.storage,
        downloader=YtDlpDownloader(runner=runner),  # type: ignore[arg-type]
    )
    _source, ingest_job = coordinator.register(URL, auto_process=False)

    coordinator.run(ingest_job)

    assert harness.jobs.list_all(kind=TRANSCRIBE_JOB_KIND) == []

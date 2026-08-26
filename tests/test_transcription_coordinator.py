"""Resumable transcription flow tests over an in-memory SQLite database."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openclips.application.ingestion import IngestionCoordinator
from openclips.application.transcription import (
    SourceNotTranscribableError,
    TranscriptionCoordinator,
)
from openclips.domain.jobs import JobStatus
from openclips.domain.sources import SourceKind
from openclips.domain.transcripts import (
    TranscriptDocument,
    TranscriptSegment,
    TranscriptWord,
)
from openclips.infrastructure.media_storage import MediaStorage, read_file_chunks
from openclips.infrastructure.models import Base, SourceAssetRecord
from openclips.infrastructure.queue import InMemoryJobQueue
from openclips.infrastructure.repositories import (
    JobRepository,
    SourceRepository,
    TranscriptRepository,
)
from openclips.providers.local_upload import LocalUploadIngestor
from openclips.providers.transcription import TranscriptionProvider
from openclips.worker import make_transcribe_handler, process_once

QUEUE_NAME = "default"


class ScriptedProvider(TranscriptionProvider):
    """Fake provider returning queued documents or raising queued errors."""

    def __init__(self) -> None:
        self.outcomes: list[TranscriptDocument | Exception] = []
        self.calls: list[Path] = []

    def is_ready(self) -> bool:
        return True

    def readiness(self) -> str:
        return "scripted provider ready"

    def transcribe(self, media_path: Path) -> TranscriptDocument:
        self.calls.append(media_path)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _document(text: str = "hello world") -> TranscriptDocument:
    word = TranscriptWord(text=text, start=0.0, end=1.0, probability=0.9)
    segment = TranscriptSegment(start=0.0, end=1.0, text=text, words=(word,))
    return TranscriptDocument(language="en", duration=1.0, segments=(segment,))


@dataclass(frozen=True)
class _Harness:
    ingestion: IngestionCoordinator
    transcription: TranscriptionCoordinator
    session: Session
    storage_root: Path


def _session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[_Harness]:
    storage_root = tmp_path / "media"
    session = _session()
    sources = SourceRepository(session)
    storage = MediaStorage(storage_root)
    yield _Harness(
        ingestion=IngestionCoordinator(sources, storage),
        transcription=TranscriptionCoordinator(
            sources=sources,
            transcripts=TranscriptRepository(session),
            jobs=JobRepository(session),
            provider=ScriptedProvider(),
            storage=storage,
        ),
        session=session,
        storage_root=storage_root,
    )
    session.close()


def _ready_source(harness: _Harness, tmp_path: Path) -> SourceAssetRecord:
    payload = tmp_path / "payload.mp4"
    payload.write_bytes(b"video-bytes")
    ingestor = LocalUploadIngestor(harness.ingestion)
    return ingestor.ingest("show.mp4", read_file_chunks(payload))


def _provider(harness: _Harness) -> ScriptedProvider:
    provider = harness.transcription.provider
    assert isinstance(provider, ScriptedProvider)
    return provider


def _sync(harness: _Harness) -> None:
    """Drop cached instances so cross-session writes become visible."""
    harness.session.expire_all()


def test_enqueue_rejects_sources_without_ready_media(harness: _Harness) -> None:
    pending = harness.ingestion.repository.create(
        source_kind=SourceKind.LOCAL_UPLOAD,
        original_locator="x.mp4",
        external_id=None,
        idempotency_key="pending-key",
        display_name="x.mp4",
        retain_until=datetime.now(UTC) + timedelta(days=7),
    )

    with pytest.raises(SourceNotTranscribableError):
        harness.transcription.enqueue(pending.id)


def test_run_persists_normalized_transcript(tmp_path: Path, harness: _Harness) -> None:
    source = _ready_source(harness, tmp_path)
    provider = _provider(harness)
    provider.outcomes.append(_document())
    job = harness.transcription.enqueue(source.id)

    record = harness.transcription.run(harness.transcription.jobs.get(job.id))  # type: ignore[arg-type]

    document = harness.transcription.transcripts.get_document(source.id)
    assert record.source_id == source.id
    assert document is not None
    assert document.full_text == "hello world"
    assert document.segments[0].words[0].probability == pytest.approx(0.9)
    assert provider.calls == [harness.storage_root / str(source.media_path)]


def test_failed_job_records_error_and_retry_reruns(
    tmp_path: Path, harness: _Harness
) -> None:
    factory = sessionmaker(bind=harness.session.get_bind())
    queue = InMemoryJobQueue()
    source = _ready_source(harness, tmp_path)
    provider = _provider(harness)
    provider.outcomes.append(RuntimeError("model exploded"))
    job = harness.transcription.enqueue(source.id)
    queue.enqueue(QUEUE_NAME, str(job.id))
    harness.session.commit()
    handlers = {"transcribe": make_transcribe_handler(provider, MediaStorage(harness.storage_root))}

    process_once(session_factory=factory, handlers=handlers, queue=queue, claim_timeout_seconds=0.0)

    _sync(harness)
    failed = harness.transcription.jobs.get(job.id)
    assert failed is not None
    assert failed.status is JobStatus.FAILED
    assert failed.error is not None and "RuntimeError" in failed.error

    retried = harness.transcription.retry(job.id)
    assert retried.status is JobStatus.QUEUED
    with pytest.raises(ValueError, match="Only failed jobs"):
        harness.transcription.retry(job.id)
    harness.session.commit()

    queue.enqueue(QUEUE_NAME, str(job.id))
    provider.outcomes.append(_document("recovered"))
    handled = process_once(
        session_factory=factory, handlers=handlers, queue=queue, claim_timeout_seconds=0.0
    )

    assert handled is True
    document = harness.transcription.transcripts.get_document(source.id)
    assert document is not None and document.full_text == "recovered"
    _sync(harness)
    succeeded = harness.transcription.jobs.get(job.id)
    assert succeeded is not None and succeeded.status is JobStatus.SUCCEEDED


def test_worker_processes_queued_transcription_end_to_end(
    tmp_path: Path, harness: _Harness
) -> None:
    factory = sessionmaker(bind=harness.session.get_bind())
    queue = InMemoryJobQueue()
    provider = _provider(harness)
    provider.outcomes.append(_document())
    source = _ready_source(harness, tmp_path)
    job = harness.transcription.enqueue(source.id)
    queue.enqueue(QUEUE_NAME, str(job.id))
    harness.session.commit()

    handled = process_once(
        session_factory=factory,
        handlers={
            "transcribe": make_transcribe_handler(provider, MediaStorage(harness.storage_root))
        },
        queue=queue,
        claim_timeout_seconds=0.0,
    )

    assert handled is True
    with factory() as session:
        transcripts = TranscriptRepository(session)
        jobs = JobRepository(session)
        refreshed = jobs.get(job.id)
        assert refreshed is not None and refreshed.status is JobStatus.SUCCEEDED
        assert transcripts.get_document(source.id) is not None


def test_worker_marks_unknown_job_kind_failed(tmp_path: Path, harness: _Harness) -> None:
    factory = sessionmaker(bind=harness.session.get_bind())
    queue = InMemoryJobQueue()
    source = _ready_source(harness, tmp_path)
    job = harness.transcription.enqueue(source.id)
    queue.enqueue(QUEUE_NAME, str(job.id))
    harness.session.commit()

    handled = process_once(
        session_factory=factory,
        handlers={},
        queue=queue,
        claim_timeout_seconds=0.0,
    )

    assert handled is True
    with factory() as session:
        refreshed = JobRepository(session).get(job.id)
        assert refreshed is not None
        assert refreshed.status is JobStatus.FAILED
        assert refreshed.error is not None and "UnknownJobKindError" in refreshed.error


def test_worker_returns_false_when_queue_is_empty() -> None:
    factory = sessionmaker(bind=create_engine("sqlite://"))
    handled = process_once(
        session_factory=factory,
        handlers={},
        queue=InMemoryJobQueue(),
        claim_timeout_seconds=0.0,
    )

    assert handled is False

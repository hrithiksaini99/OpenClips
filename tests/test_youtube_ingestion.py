"""Durable YouTube registration and background download execution."""

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openclips.application.youtube_ingestion import (
    INGEST_YOUTUBE_JOB_KIND,
    YouTubeIngestionCoordinator,
)
from openclips.domain.outbox import OutboxStatus
from openclips.domain.sources import SourceKind, SourceStatus
from openclips.infrastructure.media_storage import MediaStorage
from openclips.infrastructure.models import Base, JobRecord, OutboxRecord, SourceAssetRecord
from openclips.infrastructure.repositories import JobRepository, SourceRepository
from openclips.providers.youtube import YouTubeDownloadError, YtDlpDownloader

URL = "https://youtu.be/dQw4w9WgXcQ"
VIDEO_ID = "dQw4w9WgXcQ"
EXPECTED_KEY = hashlib.sha256(f"youtube:{VIDEO_ID}".encode()).hexdigest()
CONTENT = b"downloaded-bytes"


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _coordinator(
    session: Session, storage: MediaStorage, downloader: YtDlpDownloader
) -> YouTubeIngestionCoordinator:
    return YouTubeIngestionCoordinator(
        sources=SourceRepository(session),
        jobs=JobRepository(session),
        storage=storage,
        downloader=downloader,
    )


def _writing_downloader(content: bytes = CONTENT) -> YtDlpDownloader:
    def runner(argv: list[str]) -> object:
        destination = Path(argv[argv.index("-o") + 1])
        destination.write_bytes(content)

        class _Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Completed()

    return YtDlpDownloader(runner=runner)  # type: ignore[arg-type]


def _failing_downloader() -> YtDlpDownloader:
    def runner(argv: list[str]) -> object:
        class _Completed:
            returncode = 1
            stdout = ""
            stderr = "ERROR: Video unavailable"

        return _Completed()

    return YtDlpDownloader(runner=runner)  # type: ignore[arg-type]


def test_register_creates_pending_source_and_dispatched_ingest_job(
    session: Session, tmp_path: Path
) -> None:
    storage = MediaStorage(tmp_path / "media")
    coordinator = _coordinator(session, storage, _writing_downloader())

    source, job = coordinator.register(URL, auto_process=True)
    session.commit()

    assert source.source_kind is SourceKind.YOUTUBE_VIDEO
    assert source.status is SourceStatus.PENDING
    assert source.external_id == VIDEO_ID
    assert source.idempotency_key == EXPECTED_KEY
    assert source.auto_process is True
    assert job.kind == INGEST_YOUTUBE_JOB_KIND
    assert job.payload == str(source.id)
    event = session.query(OutboxRecord).filter_by(job_id=job.id).one()
    assert event.queue_name == "default"
    assert event.status is OutboxStatus.PENDING


def test_register_is_idempotent_by_video_id(session: Session, tmp_path: Path) -> None:
    storage = MediaStorage(tmp_path / "media")
    coordinator = _coordinator(session, storage, _writing_downloader())

    watch_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    first, _ = coordinator.register(watch_url, auto_process=True)
    second, _ = coordinator.register(URL, auto_process=False)
    session.commit()

    assert first.id == second.id
    assert session.query(SourceAssetRecord).count() == 1


def test_run_downloads_promotes_media_and_marks_ready(
    session: Session, tmp_path: Path
) -> None:
    storage = MediaStorage(tmp_path / "media")
    coordinator = _coordinator(session, storage, _writing_downloader())
    source, job = coordinator.register(URL, auto_process=True)
    session.commit()

    result = coordinator.run(job)
    session.commit()

    assert result.status is SourceStatus.READY
    assert result.byte_size == len(CONTENT)
    assert result.media_path is not None
    stored_path = storage.resolve(result.media_path)
    assert stored_path.read_bytes() == CONTENT
    # No leftover partial files remain under the media root.
    tmp_dir = storage.root / "tmp"
    leftover = list(tmp_dir.glob("*.partial")) if tmp_dir.exists() else []
    assert leftover == []


def test_run_reraises_downloader_error_and_leaves_source_recoverable(
    session: Session, tmp_path: Path
) -> None:
    storage = MediaStorage(tmp_path / "media")
    coordinator = _coordinator(session, storage, _failing_downloader())
    source, job = coordinator.register(URL, auto_process=True)
    session.commit()

    with pytest.raises(YouTubeDownloadError, match="Video unavailable"):
        coordinator.run(job)
    # The worker rolls the handler session back on any handler exception.
    session.rollback()

    refreshed = SourceRepository(session).get(source.id)
    assert refreshed is not None
    assert refreshed.status is SourceStatus.PENDING
    assert refreshed.media_path is None
    tmp_dir = storage.root / "tmp"
    leftover = list(tmp_dir.glob("*.partial")) if tmp_dir.exists() else []
    assert leftover == []


def test_run_rejects_missing_source_payload(session: Session, tmp_path: Path) -> None:
    storage = MediaStorage(tmp_path / "media")
    coordinator = _coordinator(session, storage, _writing_downloader())
    orphan = JobRecord(kind=INGEST_YOUTUBE_JOB_KIND, payload=None)
    session.add(orphan)
    session.flush()

    with pytest.raises(ValueError, match="payload"):
        coordinator.run(orphan)

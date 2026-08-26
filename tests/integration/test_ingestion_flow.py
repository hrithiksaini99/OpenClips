"""End-to-end ingestion flow tests exercising the coordinator, storage, and adapters."""

import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from openclips.application.ingestion import IngestionCoordinator
from openclips.domain.sources import SourceStatus
from openclips.infrastructure.media_storage import MediaStorage, read_file_chunks
from openclips.infrastructure.models import SourceAssetRecord
from openclips.infrastructure.repositories import SourceRepository
from openclips.providers.local_upload import LocalUploadIngestor, UnsupportedUploadError

pytestmark = pytest.mark.integration

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")




@pytest.fixture
def ingestor(session: Session, tmp_path: Path) -> LocalUploadIngestor:
    coordinator = IngestionCoordinator(SourceRepository(session), MediaStorage(tmp_path / "media"))
    return LocalUploadIngestor(coordinator)


def _make_tiny_mp4(destination: Path) -> None:
    subprocess.run(
        [
            str(FFMPEG),
            "-f",
            "lavfi",
            "-i",
            "color=c=red:size=128x72:duration=0.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.5",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-y",
            str(destination),
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def tiny_mp4(tmp_path: Path) -> Path:
    if not FFMPEG:
        pytest.skip("ffmpeg is not installed")
    destination = tmp_path / "input.mp4"
    _make_tiny_mp4(destination)
    return destination


def _stored_files(media_root: Path) -> list[Path]:
    return [path for path in media_root.rglob("*") if path.is_file()]


def test_local_upload_flow_is_idempotent(
    session: Session, ingestor: LocalUploadIngestor, tiny_mp4: Path, tmp_path: Path
) -> None:
    media_root = tmp_path / "media"

    first = ingestor.ingest("My Clip.MP4", read_file_chunks(tiny_mp4))
    replayed = ingestor.ingest("other-name.mov", read_file_chunks(tiny_mp4))

    assert first.id == replayed.id
    assert first.status is SourceStatus.READY
    stored = _stored_files(media_root)
    assert len(stored) == 1
    count = session.scalar(select(func.count()).select_from(SourceAssetRecord))
    assert count == 1


def test_ingested_media_is_valid_container(
    ingestor: LocalUploadIngestor, tiny_mp4: Path, tmp_path: Path
) -> None:
    if not FFPROBE:
        pytest.skip("ffprobe is not installed")
    record = ingestor.ingest("tiny.mp4", read_file_chunks(tiny_mp4))
    stored_path = tmp_path / "media" / str(record.media_path)

    probe = subprocess.run(
        [
            str(FFPROBE),
            "-v",
            "error",
            "-show_entries",
            "format=format_name",
            "-of",
            "csv=p=0",
            str(stored_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "mov" in probe.stdout or "mp4" in probe.stdout


def test_rejected_upload_creates_no_records(
    session: Session, ingestor: LocalUploadIngestor, tiny_mp4: Path
) -> None:
    with pytest.raises(UnsupportedUploadError):
        ingestor.ingest("clip.avi", read_file_chunks(tiny_mp4))

    count = session.scalar(select(func.count()).select_from(SourceAssetRecord))
    assert count == 0


def test_failed_source_retry_returns_to_pending(
    session: Session, ingestor: LocalUploadIngestor, tiny_mp4: Path, tmp_path: Path
) -> None:
    class ExplodingStorage(MediaStorage):
        def write_stream(self, key: str, chunks):  # type: ignore[no-untyped-def]
            for _ in chunks:
                break
            msg = "simulated disk failure"
            raise OSError(msg)

    coordinator = IngestionCoordinator(
        SourceRepository(session), ExplodingStorage(tmp_path / "other")
    )
    failing = LocalUploadIngestor(coordinator)

    with pytest.raises(OSError):
        failing.ingest("broken.mp4", read_file_chunks(tiny_mp4))
    failed_count = session.scalar(select(func.count()).select_from(SourceAssetRecord))
    assert failed_count == 1

    source_id = session.scalars(select(SourceAssetRecord.id)).one()
    retried = coordinator.retry(source_id)
    assert retried.status is SourceStatus.PENDING

    recovered = ingestor.ingest("broken.mp4", read_file_chunks(tiny_mp4))
    assert recovered.status is SourceStatus.READY

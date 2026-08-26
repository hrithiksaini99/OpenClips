from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from openclips.application.ingestion import IngestionCoordinator
from openclips.domain.sources import SourceKind, SourceStatus
from openclips.infrastructure.media_storage import (
    MediaStorage,
    StoredMedia,
    _validate_key,
)
from openclips.infrastructure.models import Base, SourceAssetRecord
from openclips.infrastructure.repositories import SourceRepository
from openclips.providers.local_upload import LocalUploadIngestor, UnsupportedUploadError


@dataclass(frozen=True)
class _Harness:
    ingestor: LocalUploadIngestor
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
    yield _Harness(
        ingestor=LocalUploadIngestor(
            IngestionCoordinator(SourceRepository(session), MediaStorage(storage_root))
        ),
        session=session,
        storage_root=storage_root,
    )
    session.close()


def _failing_stream(chunks: Iterable[bytes]) -> Iterator[bytes]:
    iterator = iter(chunks)
    first = next(iterator, None)
    if first is not None:
        yield first
    msg = "disk filled up mid-stream"
    raise OSError(msg)


class MidstreamFailureStorage(MediaStorage):
    def write_stream(self, key: str, chunks: Iterable[bytes]) -> StoredMedia:
        _validate_key(key)
        return super().write_stream(key, _failing_stream(chunks))


def test_ingests_mp4_upload_into_ready_record(harness: _Harness) -> None:
    record = harness.ingestor.ingest("beach-day.mp4", [b"video-bytes"])

    assert record.source_kind is SourceKind.LOCAL_UPLOAD
    assert record.status is SourceStatus.READY
    assert record.display_name == "beach-day.mp4"
    assert record.media_path is not None
    stored_file = harness.storage_root / record.media_path
    assert stored_file.is_file()
    assert stored_file.read_bytes() == b"video-bytes"


def test_ingest_accepts_uppercase_extension(harness: _Harness) -> None:
    record = harness.ingestor.ingest("CLIP.MOV", [b"mov-bytes"])

    assert record.status is SourceStatus.READY
    assert record.display_name == "CLIP.MOV"
    files = [path for path in harness.storage_root.rglob("*") if path.is_file()]
    assert len(files) == 1


@pytest.mark.parametrize("filename", ["clip.avi", "clip.mkv", "clip"])
def test_rejects_unsupported_or_missing_extension(
    harness: _Harness, filename: str
) -> None:
    with pytest.raises(UnsupportedUploadError, match=r"\.mov, \.mp4"):
        harness.ingestor.ingest(filename, [b"bytes"])


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("../../etc/passwd.MP4", "passwd.MP4"),
        ("C:\\Users\\x\\clip.mov", "clip.mov"),
    ],
)
def test_display_name_drops_client_directory_components(
    harness: _Harness, filename: str, expected: str
) -> None:
    record = harness.ingestor.ingest(filename, [b"bytes"])

    assert record.display_name == expected
    assert "/" not in record.display_name
    assert "\\" not in record.display_name


@pytest.mark.parametrize("filename", ["", ".", "..", "   "])
def test_rejects_empty_or_degenerate_filenames(harness: _Harness, filename: str) -> None:
    with pytest.raises(ValueError, match="Unsupported upload filename"):
        harness.ingestor.ingest(filename, [b"bytes"])


def test_duplicate_payload_reuses_record_and_single_file(harness: _Harness) -> None:
    first = harness.ingestor.ingest("one.mp4", [b"same-bytes"])
    second = harness.ingestor.ingest("two.mp4", [b"same-bytes"])

    assert second.id == first.id
    files = [path for path in harness.storage_root.rglob("*") if path.is_file()]
    assert len(files) == 1


def test_midstream_failure_marks_source_failed_without_partial_files(
    harness: _Harness,
) -> None:
    ingestor = LocalUploadIngestor(
        IngestionCoordinator(
            SourceRepository(harness.session),
            MidstreamFailureStorage(harness.storage_root),
        )
    )

    with pytest.raises(OSError, match="disk filled up mid-stream"):
        ingestor.ingest("truncated.mp4", [b"first-half", b"second-half"])

    records = harness.session.query(SourceAssetRecord).all()
    assert len(records) == 1
    failed = records[0]
    assert failed.status is SourceStatus.FAILED
    assert failed.error is not None and "OSError" in failed.error
    assert [path for path in harness.storage_root.rglob("*") if path.is_file()] == []

import hashlib
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openclips.application.ingestion import IngestionCoordinator
from openclips.domain.errors import InvalidTransitionError
from openclips.domain.sources import (
    SOURCE_RETENTION_DAYS,
    SourceEvent,
    SourceKind,
    SourceStatus,
)
from openclips.infrastructure.media_storage import MediaStorage
from openclips.infrastructure.models import Base, SourceAssetRecord
from openclips.infrastructure.repositories import SourceRepository

pytestmark = pytest.mark.integration


@pytest.fixture
def session():
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not configured")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value
        value.rollback()
    Base.metadata.drop_all(engine)


@pytest.fixture
def coordinator(session, tmp_path: Path):
    def clock() -> datetime:
        return datetime(2026, 8, 26, 12, 0, tzinfo=UTC)

    return IngestionCoordinator(
        repository=SourceRepository(session),
        storage=MediaStorage(root=tmp_path / "media"),
        clock=clock,
    )


def failing_chunks() -> Iterator[bytes]:
    yield b"partial-bytes"
    msg = "encoder vanished"
    raise RuntimeError(msg)


class ExplodingStorage(MediaStorage):
    def write_stream(self, key: str, chunks):  # type: ignore[no-untyped-def]
        msg = "disk exploded"
        raise OSError(msg)


def make_record_kwargs(idempotency_key: str) -> dict:
    return {
        "source_kind": SourceKind.LOCAL_UPLOAD,
        "original_locator": "clip.mp4",
        "external_id": None,
        "idempotency_key": idempotency_key,
        "display_name": "clip.mp4",
        "retain_until": datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
    }


def test_create_persists_source_asset_with_retention_deadline(session) -> None:
    repo = SourceRepository(session)

    record = repo.create(**make_record_kwargs("a" * 64))
    session.commit()

    fetched = repo.get(record.id)
    assert fetched is not None
    assert fetched.source_kind is SourceKind.LOCAL_UPLOAD
    assert fetched.original_locator == "clip.mp4"
    assert fetched.external_id is None
    assert fetched.idempotency_key == "a" * 64
    assert fetched.display_name == "clip.mp4"
    assert fetched.status is SourceStatus.PENDING
    assert fetched.media_path is None
    assert fetched.byte_size is None
    assert fetched.error is None
    assert fetched.retain_until == datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def test_get_by_idempotency_key_returns_existing_record(session) -> None:
    repo = SourceRepository(session)
    created = repo.create(**make_record_kwargs("b" * 64))
    session.commit()

    found = repo.get_by_idempotency_key("b" * 64)

    assert found is not None and found.id == created.id
    assert repo.get_by_idempotency_key("c" * 64) is None


def test_duplicate_idempotency_key_is_rejected(session) -> None:
    repo = SourceRepository(session)
    repo.create(**make_record_kwargs("d" * 64))
    session.commit()

    with pytest.raises(IntegrityError):
        repo.create(**make_record_kwargs("d" * 64))
    session.rollback()


def test_transition_applies_state_machine_and_persists_media(session) -> None:
    repo = SourceRepository(session)
    record = repo.create(**make_record_kwargs("e" * 64))

    repo.transition(record.id, SourceEvent.START)
    ready = repo.attach_media(record.id, media_path="local/ee.mp4", byte_size=42)
    session.commit()

    assert ready.status is SourceStatus.READY
    final = session.get(SourceAssetRecord, record.id)
    assert final.status is SourceStatus.READY
    assert final.byte_size == 42
    assert final.media_path == "local/ee.mp4"


def test_illegal_transition_raises_and_keeps_previous_status(session) -> None:
    repo = SourceRepository(session)
    record = repo.create(**make_record_kwargs("f" * 64))

    with pytest.raises(InvalidTransitionError):
        repo.transition(record.id, SourceEvent.SUCCEED)

    assert repo.get(record.id).status is SourceStatus.PENDING


def test_register_materializes_ready_source(coordinator, tmp_path: Path) -> None:
    record = coordinator.register(
        source_kind=SourceKind.LOCAL_UPLOAD,
        original_locator="Town Hall FINAL.MOV",
        display_name="town-hall.mov",
        chunks=[b"frame-one", b"frame-two"],
    )

    assert record.status is SourceStatus.READY
    assert record.byte_size == len(b"frame-oneframe-two")
    assert record.retain_until == datetime(2026, 8, 26, 12, 0, tzinfo=UTC) + timedelta(
        days=SOURCE_RETENTION_DAYS
    )
    stored_file = tmp_path / "media" / record.media_path
    assert stored_file.is_file()
    assert stored_file.read_bytes() == b"frame-oneframe-two"


def test_duplicate_payload_reuses_existing_source_without_new_file(
    coordinator, session, tmp_path: Path
) -> None:
    first = coordinator.register(
        source_kind=SourceKind.LOCAL_UPLOAD,
        original_locator="first.mp4",
        display_name="first.mp4",
        chunks=[b"identical-content"],
    )
    second = coordinator.register(
        source_kind=SourceKind.LOCAL_UPLOAD,
        original_locator="second-renamed.mp4",
        display_name="second.mp4",
        chunks=[b"identical-content"],
    )

    assert second.id == first.id
    media_files = [path for path in (tmp_path / "media").rglob("*") if path.is_file()]
    assert len(media_files) == 1
    assert session.query(SourceAssetRecord).count() == 1


def test_materialization_failure_marks_source_failed_and_leaves_no_partial_file(
    session, tmp_path: Path
) -> None:
    media_root = tmp_path / "media"
    media_root.mkdir()
    coordinator = IngestionCoordinator(
        repository=SourceRepository(session),
        storage=ExplodingStorage(root=media_root),
    )

    with pytest.raises(OSError, match="disk exploded"):
        coordinator.register(
            source_kind=SourceKind.LOCAL_UPLOAD,
            original_locator="broken.mp4",
            display_name="broken.mp4",
            chunks=[b"survives-spooling"],
        )

    records = session.query(SourceAssetRecord).all()
    assert len(records) == 1
    failed = records[0]
    assert failed.status is SourceStatus.FAILED
    assert failed.error is not None and "disk exploded" in failed.error
    assert list(media_root.rglob("*")) == []


def test_spooling_failure_creates_no_source_record(coordinator, session) -> None:
    with pytest.raises(RuntimeError, match="encoder vanished"):
        coordinator.register(
            source_kind=SourceKind.LOCAL_UPLOAD,
            original_locator="broken.mp4",
            display_name="broken.mp4",
            chunks=failing_chunks(),
        )

    assert session.query(SourceAssetRecord).count() == 0


def test_retry_moves_failed_source_back_to_pending(session, tmp_path: Path) -> None:
    coordinator = IngestionCoordinator(
        repository=SourceRepository(session),
        storage=ExplodingStorage(root=tmp_path / "media"),
    )
    with pytest.raises(OSError, match="disk exploded"):
        coordinator.register(
            source_kind=SourceKind.LOCAL_UPLOAD,
            original_locator="broken.mp4",
            display_name="broken.mp4",
            chunks=[b"retry-me"],
        )
    failed_id = coordinator.repository.get_by_idempotency_key(
        hashlib.sha256(b"retry-me").hexdigest()
    ).id

    retried = coordinator.retry(failed_id)

    assert retried.id == failed_id
    assert retried.status is SourceStatus.PENDING


def test_retry_of_ready_source_returns_it_unchanged(coordinator) -> None:
    ready = coordinator.register(
        source_kind=SourceKind.LOCAL_UPLOAD,
        original_locator="ok.mp4",
        display_name="ok.mp4",
        chunks=[b"fine"],
    )
    before_ready_at = ready.updated_at

    retried = coordinator.retry(ready.id)

    assert retried.id == ready.id
    assert retried.status is SourceStatus.READY
    assert retried.updated_at == before_ready_at

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from openclips.infrastructure.media_storage import MediaStorage, UnsafeMediaPathError

CONTENT = b"hello world"


@pytest.fixture
def media_root(tmp_path: Path) -> Path:
    return tmp_path / "media"


@pytest.fixture
def storage(media_root: Path) -> MediaStorage:
    return MediaStorage(root=media_root)


def failing_chunks() -> Iterator[bytes]:
    yield b"partial"
    msg = "disk exploded"
    raise RuntimeError(msg)


def test_write_stream_materializes_file_and_returns_metadata(
    storage: MediaStorage, media_root: Path
) -> None:
    stored = storage.write_stream("local/ab/cd123.mp4", [CONTENT])

    assert stored.key == "local/ab/cd123.mp4"
    assert stored.path == media_root / "local" / "ab" / "cd123.mp4"
    assert stored.size_bytes == len(CONTENT)
    assert stored.sha256 == hashlib.sha256(CONTENT).hexdigest()
    assert stored.path.is_relative_to(media_root.resolve())
    assert stored.path.read_bytes() == CONTENT


def test_write_stream_creates_nested_directories(storage: MediaStorage) -> None:
    storage.write_stream("youtube/de/adbeef.mov", [CONTENT])
    assert (storage.root / "youtube" / "de" / "adbeef.mov").is_file()


def test_write_stream_accepts_empty_payload(storage: MediaStorage) -> None:
    stored = storage.write_stream("local/empty.mp4", [])

    assert stored.size_bytes == 0
    assert stored.sha256 == hashlib.sha256(b"").hexdigest()
    assert stored.path.read_bytes() == b""


def test_rewrite_same_key_replaces_content_without_leaving_temp_files(
    storage: MediaStorage,
) -> None:
    storage.write_stream("local/dup.mp4", [b"first"])
    stored = storage.write_stream("local/dup.mp4", [b"second-payload"])

    assert stored.path.read_bytes() == b"second-payload"
    assert stored.size_bytes == len(b"second-payload")
    assert list((storage.root / "local").iterdir()) == [stored.path]


def test_rejects_absolute_key(storage: MediaStorage, tmp_path: Path) -> None:
    with pytest.raises(UnsafeMediaPathError):
        storage.write_stream(str(tmp_path / "escape.mp4"), [CONTENT])


def test_rejects_parent_traversal_keys(storage: MediaStorage) -> None:
    for key in ("../escape.mp4", "local/../../escape.mp4", "a/../..//escape.mp4"):
        with pytest.raises(UnsafeMediaPathError):
            storage.write_stream(key, [CONTENT])


def test_rejects_symlinked_directory_escaping_root(
    storage: MediaStorage, media_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    media_root.mkdir()
    (media_root / "link").symlink_to(outside)

    with pytest.raises(UnsafeMediaPathError):
        storage.write_stream("link/payload.mp4", [CONTENT])

    assert not (outside / "payload.mp4").exists()


def test_failed_stream_never_exposes_partial_final_file_and_cleans_up(
    storage: MediaStorage,
) -> None:
    with pytest.raises(RuntimeError, match="disk exploded"):
        storage.write_stream("local/broken.mp4", failing_chunks())

    target_dir = storage.root / "local"
    assert not (target_dir / "broken.mp4").exists()
    assert not target_dir.exists() or list(target_dir.iterdir()) == []


def test_successful_write_leaves_no_temp_files_behind(storage: MediaStorage) -> None:
    stored = storage.write_stream("local/nested/clean.mp4", [CONTENT])

    assert list((storage.root / "local" / "nested").iterdir()) == [stored.path]


def test_promote_file_moves_temporary_into_content_store(
    storage: MediaStorage, media_root: Path, tmp_path: Path
) -> None:
    temporary = tmp_path / "download.partial"
    temporary.write_bytes(CONTENT)

    stored = storage.promote_file("youtube_video/ab/cd123.mp4", temporary)

    assert stored.key == "youtube_video/ab/cd123.mp4"
    assert stored.path == media_root / "youtube_video" / "ab" / "cd123.mp4"
    assert stored.size_bytes == len(CONTENT)
    assert stored.sha256 == hashlib.sha256(CONTENT).hexdigest()
    assert stored.path.read_bytes() == CONTENT
    assert not temporary.exists()


def test_promote_file_rejects_unsafe_key(storage: MediaStorage, tmp_path: Path) -> None:
    temporary = tmp_path / "download.partial"
    temporary.write_bytes(CONTENT)

    with pytest.raises(UnsafeMediaPathError):
        storage.promote_file("../escape.mp4", temporary)

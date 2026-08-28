import errno
import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

import openclips.infrastructure.media_storage as media_storage
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


def snapshot_tree(root: Path) -> dict[Path, bytes | None]:
    return {
        path.relative_to(root): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


def swap_directory_for_symlink(directory: Path, outside: Path) -> Path:
    original = directory.with_name(f"{directory.name}-original")
    directory.rename(original)
    directory.symlink_to(outside, target_is_directory=True)
    return original


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


def test_write_stream_rejection_leaves_outside_tree_byte_for_byte_unchanged(
    storage: MediaStorage, media_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    (outside / "existing").mkdir(parents=True)
    (outside / "existing" / "content.bin").write_bytes(b"untouched")
    media_root.mkdir()
    (media_root / "escape").symlink_to(outside)
    before = snapshot_tree(outside)

    with pytest.raises(UnsafeMediaPathError):
        storage.write_stream("escape/nested/payload.bin", [CONTENT])

    assert snapshot_tree(outside) == before
    assert not (outside / "nested").exists()


def test_write_stream_intermediate_swap_before_temp_creation_never_mutates_outside(
    storage: MediaStorage,
    media_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intermediate = media_root / "safe"
    intermediate.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    before = snapshot_tree(outside)
    real_open = os.open
    swapped_to: Path | None = None

    def racing_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped_to
        if swapped_to is None and Path(path).name.startswith(".payload.bin."):
            swapped_to = swap_directory_for_symlink(intermediate, outside)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(media_storage.os, "open", racing_open)

    storage.write_stream("safe/payload.bin", [CONTENT])

    assert swapped_to is not None
    assert snapshot_tree(outside) == before
    assert (swapped_to / "payload.bin").read_bytes() == CONTENT


def test_write_stream_intermediate_swap_before_replace_never_mutates_outside(
    storage: MediaStorage,
    media_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intermediate = media_root / "safe"
    intermediate.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    before = snapshot_tree(outside)
    real_replace = os.replace
    swapped_to: Path | None = None

    def racing_replace(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped_to
        if swapped_to is None and Path(destination).name == "payload.bin":
            swapped_to = swap_directory_for_symlink(intermediate, outside)
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(media_storage.os, "replace", racing_replace)

    storage.write_stream("safe/payload.bin", [CONTENT])

    assert swapped_to is not None
    assert snapshot_tree(outside) == before
    assert (swapped_to / "payload.bin").read_bytes() == CONTENT


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

    assert temporary.read_bytes() == CONTENT


def test_promote_file_rejection_creates_nothing_outside_root(
    storage: MediaStorage, media_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    (outside / "existing").mkdir(parents=True)
    (outside / "existing" / "content.bin").write_bytes(b"untouched")
    media_root.mkdir()
    (media_root / "escape").symlink_to(outside)
    temporary = tmp_path / "download.partial"
    temporary.write_bytes(CONTENT)
    before = snapshot_tree(outside)

    with pytest.raises(UnsafeMediaPathError):
        storage.promote_file("escape/a/b/c.bin", temporary)

    assert snapshot_tree(outside) == before
    assert not (outside / "a").exists()
    assert temporary.read_bytes() == CONTENT


def test_promote_file_rejects_escaping_final_symlink_without_consuming_source(
    storage: MediaStorage, media_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    media_root.mkdir()
    (media_root / "payload.bin").symlink_to(outside)
    temporary = tmp_path / "download.partial"
    temporary.write_bytes(CONTENT)

    with pytest.raises(UnsafeMediaPathError):
        storage.promote_file("payload.bin", temporary)

    assert outside.read_bytes() == b"outside"
    assert temporary.read_bytes() == CONTENT


def test_promote_file_rejects_non_directory_intermediate_without_consuming_source(
    storage: MediaStorage, media_root: Path, tmp_path: Path
) -> None:
    media_root.mkdir()
    blocker = media_root / "blocked"
    blocker.write_bytes(b"not a directory")
    temporary = tmp_path / "download.partial"
    temporary.write_bytes(CONTENT)

    with pytest.raises(UnsafeMediaPathError):
        storage.promote_file("blocked/payload.bin", temporary)

    assert blocker.read_bytes() == b"not a directory"
    assert temporary.read_bytes() == CONTENT


def test_promote_file_intermediate_swap_before_replace_never_mutates_outside(
    storage: MediaStorage,
    media_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intermediate = media_root / "safe"
    intermediate.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    before = snapshot_tree(outside)
    temporary = tmp_path / "download.partial"
    temporary.write_bytes(CONTENT)
    real_replace = os.replace
    swapped_to: Path | None = None

    def racing_replace(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped_to
        if swapped_to is None and Path(destination).name == "payload.bin":
            swapped_to = swap_directory_for_symlink(intermediate, outside)
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(media_storage.os, "replace", racing_replace)

    storage.promote_file("safe/payload.bin", temporary)

    assert swapped_to is not None
    assert snapshot_tree(outside) == before
    assert (swapped_to / "payload.bin").read_bytes() == CONTENT
    assert not temporary.exists()


def test_promote_file_cross_filesystem_fallback_preserves_behavior(
    storage: MediaStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary = tmp_path / "download.partial"
    temporary.write_bytes(CONTENT)
    real_replace = os.replace
    replace_calls = 0

    def cross_filesystem_once(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 1:
            raise OSError(errno.EXDEV, "cross-device link")
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(media_storage.os, "replace", cross_filesystem_once)

    stored = storage.promote_file("safe/payload.bin", temporary)

    assert stored.path.read_bytes() == CONTENT
    assert stored.size_bytes == len(CONTENT)
    assert stored.sha256 == hashlib.sha256(CONTENT).hexdigest()
    assert replace_calls == 2
    assert not temporary.exists()


def test_promote_file_does_not_mask_non_cross_filesystem_replace_errors(
    storage: MediaStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary = tmp_path / "download.partial"
    temporary.write_bytes(CONTENT)
    real_replace = os.replace
    replace_calls = 0

    def permission_denied_once(*args: object, **kwargs: object) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 1:
            raise PermissionError(errno.EACCES, "permission denied")
        real_replace(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(media_storage.os, "replace", permission_denied_once)

    with pytest.raises(PermissionError):
        storage.promote_file("safe/payload.bin", temporary)

    assert temporary.read_bytes() == CONTENT
    assert not storage.resolve("safe/payload.bin").exists()
    assert replace_calls == 1


def test_delete_removes_contained_file_and_reports(storage: MediaStorage) -> None:
    stored = storage.write_stream("local/to-delete.mp4", [CONTENT])

    assert storage.delete(stored.key) is True
    assert not stored.path.exists()
    assert storage.delete(stored.key) is False


def test_delete_missing_nested_key_does_not_materialize_storage(storage: MediaStorage) -> None:
    assert storage.delete("missing/nested/payload.bin") is False
    assert not storage.root.exists()


def test_delete_refuses_to_unlink_through_escaping_symlink(
    storage: MediaStorage, media_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.bin"
    secret.write_bytes(CONTENT)
    media_root.mkdir()
    (media_root / "escape").symlink_to(outside)

    with pytest.raises(UnsafeMediaPathError):
        storage.delete("escape/secret.bin")

    assert secret.exists()


def test_delete_intermediate_swap_before_unlink_never_mutates_outside(
    storage: MediaStorage,
    media_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intermediate = media_root / "safe"
    intermediate.mkdir(parents=True)
    contained = intermediate / "payload.bin"
    contained.write_bytes(CONTENT)
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "payload.bin"
    external.write_bytes(b"outside")
    before = snapshot_tree(outside)
    real_unlink = os.unlink
    swapped_to: Path | None = None

    def racing_unlink(
        path: str | os.PathLike[str], *, dir_fd: int | None = None
    ) -> None:
        nonlocal swapped_to
        if swapped_to is None and Path(path).name == "payload.bin":
            swapped_to = swap_directory_for_symlink(intermediate, outside)
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(media_storage.os, "unlink", racing_unlink)

    assert storage.delete("safe/payload.bin") is True

    assert swapped_to is not None
    assert snapshot_tree(outside) == before
    assert not (swapped_to / "payload.bin").exists()

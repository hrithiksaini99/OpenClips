import hashlib
import os
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class UnsafeMediaPathError(ValueError):
    """Raised when a storage key would escape or subvert the media root."""


@dataclass(frozen=True)
class StoredMedia:
    key: str
    path: Path
    size_bytes: int
    sha256: str

_CHUNK_SIZE = 65536
_FORBIDDEN_COMPONENTS = frozenset({".", ".."})


def _validate_key(key: str) -> tuple[str, ...]:
    if not key or "\x00" in key:
        msg = f"Unsafe media storage key: {key!r}"
        raise UnsafeMediaPathError(msg)
    candidate = PurePosixPath(key)
    if candidate.is_absolute() or key.startswith("/"):
        msg = f"Absolute media storage keys are rejected: {key!r}"
        raise UnsafeMediaPathError(msg)
    parts = candidate.parts
    if not parts:
        msg = f"Unsafe media storage key: {key!r}"
        raise UnsafeMediaPathError(msg)
    for part in parts:
        if part in _FORBIDDEN_COMPONENTS:
            msg = f"Media storage key must not traverse directories: {key!r}"
            raise UnsafeMediaPathError(msg)
        if part != part.strip():
            msg = f"Media storage key components must be plain names: {key!r}"
            raise UnsafeMediaPathError(msg)
    return parts


class MediaStorage:
    """Writes streams to content-addressable keys beneath the media root.

    Writes are atomic: bytes land in a uniquely named temporary file inside the
    destination directory and are moved into place with ``os.replace`` once the
    whole stream is durable. A failure at any point removes the temporary file,
    so no partial final file is ever visible.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, key: str) -> Path:
        """Return the absolute path for a storage key after validating it."""
        parts = _validate_key(key)
        return self._root.joinpath(*parts)

    def promote_file(self, key: str, temporary_path: Path) -> StoredMedia:
        """Move an already-materialized temporary file into the content store.

        The temporary file (for example a completed download) is hashed and
        sized, then moved into place with ``os.replace`` when it shares the
        media root's filesystem, or copied and unlinked otherwise. Either way
        the source path no longer exists once the promotion succeeds.
        """
        target = self._prepare_target(key)

        hasher = hashlib.sha256()
        size = 0
        with temporary_path.open("rb") as handle:
            while chunk := handle.read(_CHUNK_SIZE):
                hasher.update(chunk)
                size += len(chunk)

        try:
            os.replace(temporary_path, target)
        except OSError:
            self.write_stream(key, read_file_chunks(temporary_path))
            temporary_path.unlink(missing_ok=True)

        return StoredMedia(key=key, path=target, size_bytes=size, sha256=hasher.hexdigest())

    def _prepare_target(self, key: str) -> Path:
        parts = _validate_key(key)
        self._root.mkdir(exist_ok=True)
        resolved_root = self._root.resolve()
        current = self._root
        for part in parts[:-1]:
            current = current / part
            if current.is_symlink() and not current.resolve().is_relative_to(resolved_root):
                msg = f"Media storage path escapes the media root: {key!r}"
                raise UnsafeMediaPathError(msg)
            current.mkdir(exist_ok=True)
            if current.is_symlink() and not current.resolve().is_relative_to(resolved_root):
                msg = f"Media storage path escapes the media root: {key!r}"
                raise UnsafeMediaPathError(msg)

        target = current / parts[-1]
        if target.is_symlink() and not target.resolve().is_relative_to(resolved_root):
            msg = f"Media storage path escapes the media root: {key!r}"
            raise UnsafeMediaPathError(msg)
        return target

    def delete(self, key: str) -> bool:
        """Remove a contained media file, returning whether it existed."""
        target = self._prepare_target(key)
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        return True

    def write_stream(self, key: str, chunks: Iterable[bytes]) -> StoredMedia:
        target = self._prepare_target(key)

        descriptor, temp_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".partial"
        )
        temp_path = Path(temp_name)
        hasher = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as handle:
                for chunk in chunks:
                    handle.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

        return StoredMedia(
            key=key,
            path=target,
            size_bytes=size,
            sha256=hasher.hexdigest(),
        )


def read_file_chunks(path: Path, chunk_size: int = _CHUNK_SIZE) -> Iterator[bytes]:
    """Yield file contents in chunks so callers can stream without loading in memory."""
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            yield chunk

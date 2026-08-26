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

    def write_stream(self, key: str, chunks: Iterable[bytes]) -> StoredMedia:
        parts = _validate_key(key)
        self._root.mkdir(parents=True, exist_ok=True)
        resolved_root = self._root.resolve()

        target = self._root.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)

        current = self._root
        for part in parts[:-1]:
            current = current / part
            if current.is_symlink() and not current.resolve().is_relative_to(resolved_root):
                msg = f"Media storage path escapes the media root: {key!r}"
                raise UnsafeMediaPathError(msg)

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

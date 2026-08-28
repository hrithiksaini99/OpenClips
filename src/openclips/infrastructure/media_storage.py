import contextlib
import errno
import hashlib
import os
import secrets
import stat
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

# The media root itself is trusted configuration, so it may legitimately be a
# symlink; everything below it is opened with O_NOFOLLOW so a concurrently
# planted symlink can never be traversed.
_ROOT_FLAGS = os.O_RDONLY | os.O_DIRECTORY
_CHILD_FLAGS = _ROOT_FLAGS | os.O_NOFOLLOW
_TEMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
_TEMP_MODE = 0o600
_TEMP_ATTEMPTS = 16

# O_NOFOLLOW reports ELOOP on Linux and EMLINK on some BSDs when a component is
# a symlink; O_DIRECTORY reports ENOTDIR when it is not a directory at all.
_CONTAINMENT_ERRNOS = frozenset({errno.ELOOP, errno.EMLINK, errno.ENOTDIR})


def _unsafe_path_error(key: str, reason: str) -> UnsafeMediaPathError:
    msg = f"Media storage path {reason}: {key!r}"
    return UnsafeMediaPathError(msg)


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

    Every filesystem effect is descriptor-relative: the root is opened once and
    each key component is opened from its parent's descriptor with O_NOFOLLOW,
    so a component swapped for a symlink after it was checked cannot redirect a
    later create, replace or unlink outside the directory that was verified.

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
        the source path no longer exists once the promotion succeeds, and a
        rejected key leaves the source exactly where the caller left it.
        """
        parts = _validate_key(key)
        name = parts[-1]
        target = self._root.joinpath(*parts)
        parent_fd = self._open_parent(parts, key, create=True)
        try:
            self._reject_escaping_link(parent_fd, name, target.parent, key)

            hasher = hashlib.sha256()
            size = 0
            with temporary_path.open("rb") as handle:
                while chunk := handle.read(_CHUNK_SIZE):
                    hasher.update(chunk)
                    size += len(chunk)

            try:
                os.replace(temporary_path, name, dst_dir_fd=parent_fd)
            except OSError:
                self.write_stream(key, read_file_chunks(temporary_path))
                temporary_path.unlink(missing_ok=True)
        finally:
            os.close(parent_fd)

        return StoredMedia(key=key, path=target, size_bytes=size, sha256=hasher.hexdigest())

    def write_stream(self, key: str, chunks: Iterable[bytes]) -> StoredMedia:
        parts = _validate_key(key)
        name = parts[-1]
        target = self._root.joinpath(*parts)
        parent_fd = self._open_parent(parts, key, create=True)
        try:
            self._reject_escaping_link(parent_fd, name, target.parent, key)

            descriptor, temp_name = _create_temporary(parent_fd, name)
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
                os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temp_name, dir_fd=parent_fd)
                raise
        finally:
            os.close(parent_fd)

        return StoredMedia(
            key=key,
            path=target,
            size_bytes=size,
            sha256=hasher.hexdigest(),
        )

    def delete(self, key: str) -> bool:
        """Remove a contained media file, returning whether it existed.

        Nothing is created on the way: a missing root or intermediate directory
        simply reports that there was no such file.
        """
        parts = _validate_key(key)
        try:
            parent_fd = self._open_parent(parts, key, create=False)
        except FileNotFoundError:
            return False
        try:
            # ``unlink`` never follows a symlink, so at worst this removes a
            # link that lives inside the verified directory, never its target.
            os.unlink(parts[-1], dir_fd=parent_fd)
        except FileNotFoundError:
            return False
        finally:
            os.close(parent_fd)
        return True

    def _open_parent(self, parts: tuple[str, ...], key: str, *, create: bool) -> int:
        """Open the directory holding ``parts[-1]``, descending one component at a time.

        Raises ``FileNotFoundError`` when ``create`` is false and any component
        is missing, and ``UnsafeMediaPathError`` when a component below the root
        is a symlink or is not a directory.
        """
        if create:
            self._root.mkdir(parents=True, exist_ok=True)
        current = os.open(self._root, _ROOT_FLAGS)
        handed_off = False
        try:
            for part in parts[:-1]:
                child = self._open_child(current, part, key, create=create)
                os.close(current)
                current = child
            handed_off = True
            return current
        finally:
            if not handed_off:
                os.close(current)

    def _open_child(self, parent_fd: int, part: str, key: str, *, create: bool) -> int:
        if create:
            # ``mkdir`` never follows a final symlink, so an existing escape
            # link reports EEXIST here and is rejected by the open below
            # instead of materializing a directory outside the root.
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, dir_fd=parent_fd)
        try:
            return os.open(part, _CHILD_FLAGS, dir_fd=parent_fd)
        except OSError as error:
            if error.errno in _CONTAINMENT_ERRNOS:
                raise _unsafe_path_error(key, "escapes the media root") from error
            raise

    def _reject_escaping_link(self, parent_fd: int, name: str, parent: Path, key: str) -> None:
        """Refuse to write over a final component that already links outside the root."""
        try:
            info = os.lstat(name, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        if not stat.S_ISLNK(info.st_mode):
            return
        destination = Path(os.path.join(parent, os.readlink(name, dir_fd=parent_fd)))
        if not destination.resolve().is_relative_to(self._root.resolve()):
            raise _unsafe_path_error(key, "escapes the media root")


def _create_temporary(parent_fd: int, name: str) -> tuple[int, str]:
    """Create an exclusively owned temporary sibling of ``name`` inside ``parent_fd``."""
    for _ in range(_TEMP_ATTEMPTS):
        temp_name = f".{name}.{secrets.token_hex(8)}.partial"
        try:
            return os.open(temp_name, _TEMP_FLAGS, _TEMP_MODE, dir_fd=parent_fd), temp_name
        except FileExistsError:
            continue
    raise FileExistsError(errno.EEXIST, "Could not create a unique temporary file", name)


def read_file_chunks(path: Path, chunk_size: int = _CHUNK_SIZE) -> Iterator[bytes]:
    """Yield file contents in chunks so callers can stream without loading in memory."""
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            yield chunk

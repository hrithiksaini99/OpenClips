import hashlib
import tempfile
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import BinaryIO
from uuid import UUID

from openclips.domain.sources import (
    SOURCE_RETENTION_DAYS,
    SourceEvent,
    SourceKind,
    SourceStatus,
)
from openclips.infrastructure.media_storage import MediaStorage
from openclips.infrastructure.models import SourceAssetRecord
from openclips.infrastructure.repositories import SourceRepository

_CHUNK_SIZE = 65536
_SAFE_SUFFIX_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789")


def _storage_key(source_kind: SourceKind, digest: str, display_name: str) -> str:
    suffix = PurePosixPath(display_name).suffix.lower()
    if not (len(suffix) > 1 and set(suffix[1:]) <= _SAFE_SUFFIX_CHARS):
        suffix = ""
    return f"{source_kind.value.lower()}/{digest[:2]}/{digest}{suffix}"


def _spool_chunks(spool: BinaryIO) -> Iterator[bytes]:
    try:
        while chunk := spool.read(_CHUNK_SIZE):
            yield chunk
    finally:
        spool.close()


def _failure_message(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


class IngestionCoordinator:
    """Registers sources idempotently and materializes their media safely.

    ``register`` hashes the incoming stream first so a duplicate payload reuses
    the existing source record without writing a second file or scheduling a
    second ingestion. New payloads create a ``PENDING`` record before
    materialization, then move through ``INGESTING`` to ``READY``; any failure
    marks the source ``FAILED`` with an actionable message and never exposes a
    partial final file.
    """

    def __init__(
        self,
        repository: SourceRepository,
        storage: MediaStorage,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self._clock = clock or (lambda: datetime.now(UTC))

    def register(
        self,
        *,
        source_kind: SourceKind,
        original_locator: str,
        display_name: str,
        chunks: Iterable[bytes],
        external_id: str | None = None,
        auto_process: bool = True,
    ) -> SourceAssetRecord:
        digest, spool = self._spool(chunks)
        try:
            existing = self.repository.get_by_idempotency_key(digest)
            if existing is not None:
                if existing.status is SourceStatus.READY:
                    return existing
                self._recover(existing.id)
                record = existing
            else:
                record = self.repository.create(
                    source_kind=source_kind,
                    original_locator=original_locator,
                    external_id=external_id,
                    idempotency_key=digest,
                    display_name=display_name,
                    retain_until=self._clock() + timedelta(days=SOURCE_RETENTION_DAYS),
                    auto_process=auto_process,
                )
            key = _storage_key(record.source_kind, digest, record.display_name)
            try:
                self.repository.transition(record.id, SourceEvent.START)
                stored = self.storage.write_stream(key, _spool_chunks(spool))
                return self.repository.attach_media(
                    record.id, media_path=stored.key, byte_size=stored.size_bytes
                )
            except BaseException as error:
                self.repository.transition(
                    record.id, SourceEvent.FAIL, error=_failure_message(error)
                )
                raise
        finally:
            if not spool.closed:
                spool.close()

    def retry(self, source_id: UUID) -> SourceAssetRecord:
        record = self.repository.get(source_id)
        if record is None:
            raise KeyError(source_id)
        if record.status == SourceStatus.READY:
            return record
        return self.repository.transition(record.id, SourceEvent.RETRY)

    def _recover(self, source_id: UUID) -> None:
        """Move an incomplete source back to PENDING so it can be ingested again."""
        record = self.repository.get(source_id)
        if record is None:
            raise KeyError(source_id)
        if record.status is SourceStatus.PENDING:
            return
        if record.status is SourceStatus.INGESTING:
            self.repository.transition(
                record.id, SourceEvent.FAIL, error="Recovered from interrupted ingestion"
            )
        self.repository.transition(source_id, SourceEvent.RETRY)

    def _spool(self, chunks: Iterable[bytes]) -> tuple[str, BinaryIO]:
        hasher = hashlib.sha256()
        spool = tempfile.TemporaryFile()  # noqa: SIM115
        try:
            for chunk in chunks:
                hasher.update(chunk)
                spool.write(chunk)
            spool.seek(0)
        except BaseException:
            spool.close()
            raise
        return hasher.hexdigest(), spool

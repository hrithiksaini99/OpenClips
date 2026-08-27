"""Durable YouTube source registration and background download execution."""

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from openclips.application.pipeline import queue_for_job_kind
from openclips.domain.sources import (
    SOURCE_RETENTION_DAYS,
    SourceEvent,
    SourceKind,
    SourceStatus,
)
from openclips.infrastructure.media_storage import MediaStorage
from openclips.infrastructure.models import JobRecord, SourceAssetRecord
from openclips.infrastructure.repositories import JobRepository, SourceRepository
from openclips.providers.youtube import (
    YtDlpDownloader,
    canonicalize_youtube_url,
    extract_youtube_video_id,
)

INGEST_YOUTUBE_JOB_KIND = "ingest_youtube"


def youtube_idempotency_key(video_id: str) -> str:
    """Derive the stable per-video idempotency key used for source de-duplication."""
    return hashlib.sha256(f"youtube:{video_id}".encode()).hexdigest()


class YouTubeIngestionCoordinator:
    """Registers YouTube videos as durable sources and downloads them in the worker.

    ``register`` is a fast, side-effect-light API call: it records a ``PENDING``
    source and a dispatched ``ingest_youtube`` job, then returns so the HTTP
    caller never waits on a download. ``run`` executes that job in the worker,
    streaming the video to a uniquely named partial file, promoting it into the
    content store, and marking the source ``READY``. A failure re-raises so the
    worker rolls the handler session back — leaving the source ``PENDING`` and
    retryable through the job — while always cleaning up the partial file.
    """

    def __init__(
        self,
        *,
        sources: SourceRepository,
        jobs: JobRepository,
        storage: MediaStorage,
        downloader: YtDlpDownloader,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sources = sources
        self.jobs = jobs
        self.storage = storage
        self.downloader = downloader
        self._clock = clock or (lambda: datetime.now(UTC))

    def register(self, url: str, auto_process: bool) -> tuple[SourceAssetRecord, JobRecord]:
        """Record a pending YouTube source and dispatch its background ingest job."""
        video_id = extract_youtube_video_id(url)
        canonical = canonicalize_youtube_url(url)
        key = youtube_idempotency_key(video_id)

        source = self.sources.get_by_idempotency_key(key)
        if source is None:
            source = self.sources.create(
                source_kind=SourceKind.YOUTUBE_VIDEO,
                original_locator=canonical,
                external_id=video_id,
                idempotency_key=key,
                display_name=f"{video_id}.mp4",
                retain_until=self._clock() + timedelta(days=SOURCE_RETENTION_DAYS),
                auto_process=auto_process,
            )
        job, _event = self.jobs.create_dispatched(
            INGEST_YOUTUBE_JOB_KIND,
            payload=str(source.id),
            queue_name=queue_for_job_kind(INGEST_YOUTUBE_JOB_KIND),
        )
        return source, job

    def run(self, job: JobRecord) -> SourceAssetRecord:
        """Execute one claimed ingest job body: download, promote, and mark ready.

        Source state advances only on the success path (``PENDING -> INGESTING
        -> READY``). Any failure re-raises so the worker rolls the handler
        session back — leaving the source ``PENDING`` and retryable through the
        job — and preserves the downloader's error on the job. The partial file
        is always removed, since it lives outside the transaction.
        """
        if job.payload is None:
            msg = f"Ingest job {job.id} has no source payload"
            raise ValueError(msg)
        source_id = UUID(job.payload)
        source = self.sources.get(source_id)
        if source is None:
            raise KeyError(source_id)

        partial = self.storage.root / "tmp" / f"{uuid4().hex}.partial"
        partial.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.sources.transition(source_id, SourceEvent.START)
            self.downloader.download(source.original_locator, partial)
            key = self._storage_key(source)
            stored = self.storage.promote_file(key, partial)
            return self.sources.attach_media(
                source_id, media_path=stored.key, byte_size=stored.size_bytes
            )
        finally:
            partial.unlink(missing_ok=True)

    def _storage_key(self, source: SourceAssetRecord) -> str:
        digest = source.idempotency_key
        return f"{SourceKind.YOUTUBE_VIDEO.value.lower()}/{digest[:2]}/{digest}.mp4"

    @staticmethod
    def is_ready(source: SourceAssetRecord) -> bool:
        return source.status is SourceStatus.READY

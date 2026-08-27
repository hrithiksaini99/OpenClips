"""Transcription orchestration: validated sources become normalized transcripts."""

from uuid import UUID

from openclips.application.pipeline import queue_for_job_kind
from openclips.domain.jobs import JobStatus
from openclips.domain.sources import SourceStatus
from openclips.domain.transcripts import TranscriptDocument
from openclips.infrastructure.media_storage import MediaStorage
from openclips.infrastructure.models import JobRecord, SourceAssetRecord, TranscriptRecord
from openclips.infrastructure.repositories import (
    JobRepository,
    SourceRepository,
    TranscriptRepository,
)
from openclips.providers.transcription import TranscriptionProvider

TRANSCRIBE_JOB_KIND = "transcribe"


class SourceNotTranscribableError(ValueError):
    """Raised when a source has no ready media available for transcription."""


class TranscriptionCoordinator:
    """Enqueues transcription jobs and runs their bodies inside a session.

    Lifecycle transitions belong to the worker; this coordinator validates
    inputs, executes the provider, and persists normalized transcripts so a
    retried job replaces its previous output deterministically.
    """

    def __init__(
        self,
        *,
        sources: SourceRepository,
        transcripts: TranscriptRepository,
        jobs: JobRepository,
        provider: TranscriptionProvider,
        storage: MediaStorage,
    ) -> None:
        self.sources = sources
        self.transcripts = transcripts
        self.jobs = jobs
        self.provider = provider
        self.storage = storage

    def enqueue(self, source_id: UUID) -> JobRecord:
        """Create a queued transcription job for a ready source."""
        self._transcribable_source(source_id)
        job, _event = self.jobs.create_dispatched(
            TRANSCRIBE_JOB_KIND,
            payload=str(source_id),
            queue_name=queue_for_job_kind(TRANSCRIBE_JOB_KIND),
        )
        return job

    def run(self, job: JobRecord) -> TranscriptRecord:
        """Execute one claimed transcription job body without touching state."""
        source = self._job_source(job)
        document: TranscriptDocument = self.provider.transcribe(
            self.storage.resolve(str(source.media_path))
        )
        return self.transcripts.upsert_for_source(source.id, document)

    def retry(self, job_id: UUID) -> JobRecord:
        """Requeue a failed transcription job so it can be executed again."""
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.status is not JobStatus.FAILED:
            msg = f"Only failed jobs can be retried; job {job_id} is {job.status}"
            raise ValueError(msg)
        retried, _event = self.jobs.retry_dispatched(job.id)
        return retried

    def _transcribable_source(self, source_id: UUID) -> SourceAssetRecord:
        source = self.sources.get(source_id)
        if source is None:
            raise KeyError(source_id)
        if source.status is not SourceStatus.READY or not source.media_path:
            msg = f"Source {source_id} is not ready for transcription ({source.status})"
            raise SourceNotTranscribableError(msg)
        return source

    def _job_source(self, job: JobRecord) -> SourceAssetRecord:
        if not job.payload:
            msg = f"Transcription job {job.id} has no source payload"
            raise SourceNotTranscribableError(msg)
        return self._transcribable_source(UUID(job.payload))

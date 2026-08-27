"""Clip selection orchestration: transcripts become reviewable clip records."""

import logging
from uuid import UUID

from openclips.application.pipeline import queue_for_job_kind
from openclips.application.selection import build_candidates
from openclips.domain.clips import ClipEvent
from openclips.domain.selection import SelectionBounds
from openclips.domain.sources import SourceStatus
from openclips.infrastructure.models import ClipRecord, JobRecord, SourceAssetRecord
from openclips.infrastructure.repositories import (
    ClipRepository,
    JobRepository,
    SourceRepository,
    TranscriptRepository,
)
from openclips.providers.llm import ClipRefiner, MalformedModelOutputError

logger = logging.getLogger(__name__)

SELECT_CLIPS_JOB_KIND = "select_clips"


class TranscriptMissingError(ValueError):
    """Raised when a source has no persisted transcript to select from."""


class ClipSelectionCoordinator:
    """Creates deterministic, bounded clip candidates for a transcribed source.

    Selection runs on the persisted transcript; refiner output that cannot be
    trusted is rejected safely by falling back to the heuristic candidates.
    Re-running a source replaces its previous clips deterministically.
    """

    def __init__(
        self,
        *,
        sources: SourceRepository,
        transcripts: TranscriptRepository,
        clips: ClipRepository,
        jobs: JobRepository,
        refiner: ClipRefiner,
        bounds: SelectionBounds | None = None,
    ) -> None:
        self.sources = sources
        self.transcripts = transcripts
        self.clips = clips
        self.jobs = jobs
        self.refiner = refiner
        self.bounds = bounds or SelectionBounds()

    def enqueue(self, source_id: UUID) -> JobRecord:
        """Create a queued clip-selection job for a ready, transcribed source."""
        self._selected_source(source_id)
        if self.transcripts.get_document(source_id) is None:
            msg = f"Source {source_id} has no transcript to select from"
            raise TranscriptMissingError(msg)
        job, _event = self.jobs.create_dispatched(
            SELECT_CLIPS_JOB_KIND,
            payload=str(source_id),
            queue_name=queue_for_job_kind(SELECT_CLIPS_JOB_KIND),
        )
        return job

    def run(self, job: JobRecord) -> list[ClipRecord]:
        """Execute one claimed selection job body without touching state."""
        if not job.payload:
            msg = f"Clip selection job {job.id} has no source payload"
            raise TranscriptMissingError(msg)
        return self.select_for_source(UUID(job.payload))

    def select_for_source(self, source_id: UUID) -> list[ClipRecord]:
        source = self._selected_source(source_id)
        document = self.transcripts.get_document(source.id)
        if document is None:
            msg = f"Source {source_id} has no transcript to select from"
            raise TranscriptMissingError(msg)

        candidates = build_candidates(document, self.bounds)
        try:
            candidates = self.refiner.refine(candidates, document, self.bounds)
        except MalformedModelOutputError:
            logger.warning(
                "Refiner output for source %s was malformed; keeping heuristic candidates",
                source_id,
            )
            candidates = build_candidates(document, self.bounds)

        self.clips.delete_for_source(source.id)
        records = []
        for candidate in candidates:
            record = self.clips.create(
                source_asset_id=source.id,
                title=candidate.title,
                start_time=candidate.start,
                end_time=candidate.end,
                selection_score=candidate.score,
            )
            self.clips.transition(record.id, ClipEvent.READY)
            records.append(record)
        return records

    def _selected_source(self, source_id: UUID) -> SourceAssetRecord:
        source = self.sources.get(source_id)
        if source is None:
            raise KeyError(source_id)
        if source.status is not SourceStatus.READY:
            msg = f"Source {source_id} is not ready for selection ({source.status})"
            raise TranscriptMissingError(msg)
        return source

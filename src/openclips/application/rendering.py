"""Render orchestration: clips become persisted vertical media with captions."""

import logging
from pathlib import Path
from uuid import UUID

from openclips.domain.captions import CaptionStyle
from openclips.domain.clips import ClipStatus
from openclips.domain.jobs import JobEvent, JobStatus
from openclips.domain.transcripts import TranscriptDocument
from openclips.infrastructure.media_storage import MediaStorage
from openclips.infrastructure.models import ClipRecord, JobRecord
from openclips.infrastructure.repositories import (
    ClipRepository,
    JobRepository,
    SourceRepository,
    TranscriptRepository,
)
from openclips.providers.captions import CaptionEdit, build_ass, build_srt, prepare_words
from openclips.providers.renderer import (
    RENDER_HEIGHT,
    RENDER_WIDTH,
    CenterCropStrategy,
    CropStrategy,
    FFmpegRenderer,
    RenderRequest,
)

logger = logging.getLogger(__name__)

RENDER_CLIP_JOB_KIND = "render_clip"


class ClipNotRenderableError(ValueError):
    """Raised when a clip cannot be rendered in its current state."""


class ClipSelectionMissingError(ValueError):
    """Raised when a clip lacks the timespan data required for rendering."""


def _failure_message(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


class RenderCoordinator:
    """Executes resumable render jobs that attach media artifacts to clips.

    The clip lifecycle stays untouched; durability comes from the job record.
    A retried render replaces the previous outputs deterministically.
    """

    def __init__(
        self,
        *,
        clips: ClipRepository,
        sources: SourceRepository,
        transcripts: TranscriptRepository,
        jobs: JobRepository,
        renderer: FFmpegRenderer,
        storage: MediaStorage,
        style: CaptionStyle,
        crop_strategy: CropStrategy | None = None,
        edits: tuple[CaptionEdit, ...] = (),
        mask_words: frozenset[str] = frozenset(),
        width: int = RENDER_WIDTH,
        height: int = RENDER_HEIGHT,
    ) -> None:
        self.clips = clips
        self.sources = sources
        self.transcripts = transcripts
        self.jobs = jobs
        self.renderer = renderer
        self.storage = storage
        self.style = style
        self.crop_strategy = crop_strategy or CenterCropStrategy()
        self.edits = edits
        self.mask_words = mask_words
        self.width = width
        self.height = height

    def enqueue(self, clip_id: UUID) -> JobRecord:
        """Create a queued render job for a reviewable clip."""
        self._renderable_clip(clip_id)
        return self.jobs.create(RENDER_CLIP_JOB_KIND, payload=str(clip_id))

    def run(self, job: JobRecord) -> ClipRecord:
        """Execute one claimed render job body without touching state."""
        if not job.payload:
            msg = f"Render job {job.id} has no clip payload"
            raise ClipNotRenderableError(msg)
        return self.render_clip(UUID(job.payload))

    def retry(self, job_id: UUID) -> JobRecord:
        """Requeue a failed render job so it can be executed again."""
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.status is JobStatus.FAILED:
            return self.jobs.transition(job.id, JobEvent.RETRY)
        msg = f"Only failed jobs can be retried; job {job_id} is {job.status}"
        raise ValueError(msg)

    def render_clip(self, clip_id: UUID) -> ClipRecord:
        """Generate captions plus vertical media and persist their paths."""
        clip = self._renderable_clip(clip_id)
        document = self._clip_document(clip)
        clip_start = float(clip.start_time or 0.0)
        clip_end = float(clip.end_time or 0.0)

        words = prepare_words(
            document,
            clip_start,
            clip_end,
            edits=self.edits,
            mask_words=self.mask_words,
        )

        srt_text = build_srt(words, uppercase=self.style.uppercase)
        srt_key = f"clips/{clip.id}/captions.{self.style.name}.srt"
        stored_srt = self.storage.write_stream(srt_key, iter([srt_text.encode()]))

        if self.style.word_highlight:
            ass_text = build_ass(
                words,
                self.style,
                width=self.width,
                height=self.height,
                clip_offset=clip_start,
            )
            ass_key = f"clips/{clip.id}/captions.{self.style.name}.ass"
            stored_caption = self.storage.write_stream(ass_key, iter([ass_text.encode()]))
            subtitle_media_path: Path | None = Path(stored_caption.path)
        else:
            stored_caption = stored_srt
            subtitle_media_path = Path(stored_srt.path)

        source_media = self.storage.resolve(str(self._source_media_path(clip)))
        output_key = f"clips/{clip.id}/render_{self.style.name}_{self.width}x{self.height}.mp4"
        output_path = self.storage.root / output_key
        request = RenderRequest(
            source_media=source_media,
            output_media=output_path,
            start=clip_start,
            end=clip_end,
            subtitle_path=subtitle_media_path,
            width=self.width,
            height=self.height,
        )
        argv = self.renderer.render(request, self.crop_strategy)
        logger.info("Rendered clip %s with %d command tokens", clip.id, len(argv))

        clip.output_path = output_key
        clip.caption_path = stored_caption.key
        clip.caption_template = self.style.name
        clip.render_width = self.width
        clip.render_height = self.height
        self.clips.session.flush()
        return clip

    def _renderable_clip(self, clip_id: UUID) -> ClipRecord:
        clip = self.clips.get(clip_id)
        if clip is None:
            raise KeyError(clip_id)
        if clip.status not in (ClipStatus.READY_FOR_REVIEW, ClipStatus.NEEDS_REVIEW):
            msg = f"Clip {clip_id} is not renderable from status {clip.status}"
            raise ClipNotRenderableError(msg)
        if clip.start_time is None or clip.end_time is None:
            msg = f"Clip {clip_id} has no selection timespan"
            raise ClipSelectionMissingError(msg)
        return clip

    def _source_media_path(self, clip: ClipRecord) -> str:
        if clip.source_asset_id is None:
            msg = f"Clip {clip.id} has no source asset"
            raise ClipNotRenderableError(msg)
        source = self.sources.get(clip.source_asset_id)
        if source is None or not source.media_path:
            msg = f"Source for clip {clip.id} has no ready media"
            raise ClipNotRenderableError(msg)
        return source.media_path

    def _clip_document(self, clip: ClipRecord) -> TranscriptDocument:
        if clip.source_asset_id is None:
            msg = f"Clip {clip.id} has no source asset"
            raise ClipSelectionMissingError(msg)
        document = self.transcripts.get_document(clip.source_asset_id)
        if document is None:
            msg = f"No transcript available for clip {clip.id}"
            raise ClipSelectionMissingError(msg)
        return document

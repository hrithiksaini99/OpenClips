"""Render coordinator flow tests over an in-memory SQLite database."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openclips.application.rendering import (
    RENDER_CLIP_JOB_KIND,
    ClipNotRenderableError,
    RenderCoordinator,
)
from openclips.domain.captions import get_template
from openclips.domain.clips import ClipEvent
from openclips.domain.jobs import JobStatus
from openclips.domain.selection import ClipCandidate
from openclips.domain.sources import SourceEvent, SourceKind
from openclips.domain.transcripts import TranscriptDocument, TranscriptSegment, TranscriptWord
from openclips.infrastructure.media_storage import MediaStorage
from openclips.infrastructure.models import Base
from openclips.infrastructure.queue import InMemoryJobQueue
from openclips.infrastructure.repositories import (
    ClipRepository,
    JobRepository,
    SourceRepository,
    TranscriptRepository,
)
from openclips.providers.captions import CaptionEdit
from openclips.providers.renderer import FFmpegRenderer
from openclips.worker import make_render_handler, process_once

QUEUE_NAME = "default"


def _word(text: str, start: float, end: float) -> TranscriptWord:
    return TranscriptWord(text=text, start=start, end=end, probability=0.95)


def _document() -> TranscriptDocument:
    words = (
        _word("The", 0.0, 0.3),
        _word("secret", 0.3, 0.8),
        _word("is", 0.8, 1.0),
        _word("persistence", 1.0, 2.0),
        _word("every", 22.0, 22.5),
        _word("single", 22.5, 23.0),
        _word("day", 23.0, 23.6),
    )
    segment = TranscriptSegment(
        start=0.0, end=24.0, text=" ".join(word.text for word in words), words=words
    )
    return TranscriptDocument(language="en", duration=24.0, segments=(segment,))


class FakeRenderer(FFmpegRenderer):
    """Renderer double that writes a placeholder artifact instead of media."""

    def __init__(self, *, fail_first: bool = False) -> None:
        super().__init__(runner=self._run)  # type: ignore[arg-type]
        self.fail_first = fail_first
        self.calls: list[list[str]] = []

    def _run(self, argv):  # type: ignore[no-untyped-def]
        if argv[0] == "ffprobe":
            payload = (
                '{"format": {"duration": "60.0"}, '
                '"streams": [{"codec_type": "video", "width": 1920, "height": 1080}]}'
            )
            return subprocess_completed(0, stdout=payload)
        self.calls.append(list(argv))
        if self.fail_first and len(self.calls) == 1:
            return subprocess_completed(1, stderr="broken encoder")
        output = argv[-1]
        with open(output, "wb") as handle:
            handle.write(b"fake-vertical-media")
        return subprocess_completed(0)


def subprocess_completed(
    returncode: int, stdout: str = "", stderr: str = ""
):
    from subprocess import CompletedProcess

    return CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@dataclass(frozen=True)
class _Harness:
    clips: ClipRepository
    sources: SourceRepository
    transcripts: TranscriptRepository
    jobs: JobRepository
    storage: MediaStorage
    session: Session


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[_Harness]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield _Harness(
            clips=ClipRepository(session),
            sources=SourceRepository(session),
            transcripts=TranscriptRepository(session),
            jobs=JobRepository(session),
            storage=MediaStorage(tmp_path / "media"),
            session=session,
        )


def _reviewable_clip(harness: _Harness) -> object:
    source_record = harness.sources.create(
        source_kind=SourceKind.LOCAL_UPLOAD,
        original_locator="show.mp4",
        external_id=None,
        idempotency_key="render-key",
        display_name="show.mp4",
        retain_until=datetime.now(UTC) + timedelta(days=7),
    )
    harness.sources.transition(source_record.id, SourceEvent.START)
    ready_source = harness.sources.attach_media(
        source_record.id, media_path="local_upload/show.mp4", byte_size=99
    )
    harness.transcripts.upsert_for_source(ready_source.id, _document())
    clip = harness.clips.create(
        source_asset_id=ready_source.id,
        title="Persistence pays",
        start_time=0.0,
        end_time=24.0,
        selection_score=2.5,
    )
    return harness.clips.transition(clip.id, ClipEvent.READY)


def _coordinator(harness: _Harness, renderer: FFmpegRenderer, tmp_path: Path) -> RenderCoordinator:
    del tmp_path
    return RenderCoordinator(
        clips=harness.clips,
        sources=harness.sources,
        transcripts=harness.transcripts,
        jobs=harness.jobs,
        renderer=renderer,
        storage=harness.storage,
        style=get_template("karaoke"),
        edits=(CaptionEdit("secret", "truth"),),
        mask_words=frozenset({"damn"}),
        width=1080,
        height=1920,
    )


def test_render_persists_media_and_caption_artifacts(harness: _Harness, tmp_path: Path) -> None:
    clip = _reviewable_clip(harness)
    assert isinstance(clip, object)
    coordinator = _coordinator(harness, FakeRenderer(), tmp_path)

    job = coordinator.enqueue(clip.id)  # type: ignore[attr-defined]
    rendered = coordinator.run(coordinator.jobs.get(job.id))  # type: ignore[arg-type]

    assert rendered.output_path is not None
    output_file = harness.storage.root / str(rendered.output_path)
    assert output_file.read_bytes() == b"fake-vertical-media"
    caption_file = harness.storage.root / str(rendered.caption_path)
    assert caption_file.suffix == ".ass"
    caption_text = caption_file.read_text()
    assert "{\\kf" in caption_text
    assert "TRUTH" in caption_text and "SECRET" not in caption_text
    assert rendered.caption_template == "karaoke"
    assert rendered.render_width == 1080 and rendered.render_height == 1920


def test_failed_render_is_resumable_through_job_retry(
    harness: _Harness, tmp_path: Path
) -> None:
    factory = sessionmaker(bind=harness.session.get_bind())
    queue = InMemoryJobQueue()
    clip = _reviewable_clip(harness)
    harness.session.commit()
    renderer = FakeRenderer(fail_first=True)
    coordinator = _coordinator(harness, renderer, tmp_path)

    job = coordinator.enqueue(clip.id)  # type: ignore[attr-defined]
    harness.session.commit()
    queue.enqueue(QUEUE_NAME, str(job.id))
    handlers = {
        RENDER_CLIP_JOB_KIND: make_render_handler(
            renderer, harness.storage, get_template("karaoke"), 1080, 1920
        )
    }
    process_once(session_factory=factory, handlers=handlers, queue=queue, claim_timeout_seconds=0.0)

    harness.session.expire_all()
    failed = harness.jobs.get(job.id)
    assert failed is not None
    assert failed.status is JobStatus.FAILED
    assert failed.error is not None and "RenderError" in failed.error

    retried = coordinator.retry(job.id)
    assert retried.status is JobStatus.QUEUED
    harness.session.commit()
    queue.enqueue(QUEUE_NAME, str(job.id))
    handled = process_once(
        session_factory=factory, handlers=handlers, queue=queue, claim_timeout_seconds=0.0
    )

    assert handled is True
    harness.session.expire_all()
    succeeded = harness.jobs.get(job.id)
    assert succeeded is not None
    assert succeeded.status is JobStatus.SUCCEEDED


def test_enqueue_rejects_clips_without_review_status(harness: _Harness) -> None:
    source_record = harness.sources.create(
        source_kind=SourceKind.LOCAL_UPLOAD,
        original_locator="x.mp4",
        external_id=None,
        idempotency_key="render-pending",
        display_name="x.mp4",
        retain_until=datetime.now(UTC) + timedelta(days=7),
    )
    pending_clip = harness.clips.create(source_asset_id=source_record.id)
    coordinator = _coordinator(harness, FakeRenderer(), Path("/tmp"))

    with pytest.raises(ClipNotRenderableError):
        coordinator.enqueue(pending_clip.id)


def test_candidate_helper_stays_importable() -> None:
    candidate = ClipCandidate(start=0.0, end=30.0, title="t", score=1.0, segment_indices=(0,))

    assert candidate.duration == pytest.approx(30.0)

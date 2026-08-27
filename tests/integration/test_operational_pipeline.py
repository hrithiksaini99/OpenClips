"""Real PostgreSQL/Redis/FFmpeg proof that one upload runs to reviewable clips.

Drives the actual durable path: the authenticated upload API records a source
and outbox intent, the relay moves due events onto reliable Redis lists, and the
worker consumes them, chaining transcribe -> select -> render automatically. The
transcription provider is the only injected fake; selection and rendering use the
real deterministic selector and FFmpeg.
"""

import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from openclips.application.clipping import SELECT_CLIPS_JOB_KIND
from openclips.application.dispatch import OutboxRelay
from openclips.application.rendering import RENDER_CLIP_JOB_KIND
from openclips.application.services import build_services
from openclips.application.transcription import TRANSCRIBE_JOB_KIND
from openclips.config import Settings
from openclips.domain.captions import get_template
from openclips.domain.clips import ClipStatus
from openclips.domain.selection import SelectionBounds
from openclips.domain.sources import SourceStatus
from openclips.domain.transcripts import TranscriptDocument, TranscriptSegment, TranscriptWord
from openclips.infrastructure.media_storage import MediaStorage
from openclips.infrastructure.queue import RedisJobQueue
from openclips.infrastructure.repositories import (
    ClipRepository,
    SourceRepository,
    TranscriptRepository,
)
from openclips.main import create_app
from openclips.providers.llm import HeuristicClipRefiner
from openclips.providers.renderer import FFmpegRenderer
from openclips.providers.transcription import TranscriptionProvider
from openclips.worker import (
    Handler,
    make_render_handler,
    make_select_clips_handler,
    make_transcribe_handler,
    process_once,
)

pytestmark = pytest.mark.integration

FFMPEG = shutil.which("ffmpeg")
TOKEN = "test-admin-token"
QUEUES = ("default",)


class _DeterministicProvider(TranscriptionProvider):
    """Returns a fixed transcript whose word timings fit inside the fixture media."""

    def is_ready(self) -> bool:
        return True

    def readiness(self) -> str:
        return "deterministic provider ready"

    def transcribe(self, media_path: Path) -> TranscriptDocument:
        del media_path
        return _document()


def _document() -> TranscriptDocument:
    lines = [
        "The single biggest money mistake is never tracking your spending at all.",
        "Here is the one budgeting trick that quietly changes everything for you.",
        "Write down every expense for a week and the leaks reveal themselves fast.",
    ]
    segments = []
    for index, text in enumerate(lines):
        start = index * 8.0
        words = tuple(
            TranscriptWord(
                text=token.strip(".,!?;:"),
                start=start + offset * 0.4,
                end=start + offset * 0.4 + 0.35,
                probability=0.95,
            )
            for offset, token in enumerate(text.split())
        )
        segments.append(TranscriptSegment(start=start, end=start + 7.5, text=text, words=words))
    return TranscriptDocument(language="en", duration=24.0, segments=tuple(segments))


def _make_mp4(destination: Path) -> None:
    subprocess.run(
        [
            str(FFMPEG),
            "-f", "lavfi", "-i", "color=c=teal:size=640x360:duration=25",
            "-f", "lavfi", "-i", "sine=frequency=220:duration=25",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", "-y", str(destination),
        ],
        check=True,
        capture_output=True,
    )


def _redis_queue() -> RedisJobQueue:
    url = os.getenv("REDIS_URL")
    if not url:
        pytest.skip("REDIS_URL is not configured")
    import redis

    client = redis.Redis.from_url(url)
    client.flushdb()
    return RedisJobQueue(client)


def _handlers(media_root: Path) -> dict[str, Handler]:
    storage = MediaStorage(media_root)
    bounds = SelectionBounds(max_clips=3, min_duration_seconds=3.0, max_duration_seconds=12.0)
    return {
        TRANSCRIBE_JOB_KIND: make_transcribe_handler(_DeterministicProvider(), storage),  # type: ignore[arg-type]
        SELECT_CLIPS_JOB_KIND: make_select_clips_handler(HeuristicClipRefiner(), bounds),
        RENDER_CLIP_JOB_KIND: make_render_handler(
            FFmpegRenderer(), storage, get_template("clean"), 1080, 1920
        ),
    }


def _drain(
    relay: OutboxRelay,
    session_factory: sessionmaker[Session],
    queue: RedisJobQueue,
    handlers: dict[str, Handler],
) -> None:
    """Alternate relay and consume until the pipeline quiesces (bounded)."""
    for _ in range(64):
        relay.dispatch_once()
        handled = process_once(
            session_factory=session_factory,
            handlers=handlers,
            queue=queue,
            queue_names=QUEUES,
            claim_timeout_seconds=1,
        )
        if not handled and relay.dispatch_once() == 0 and queue.depth("default") == 0:
            break


def test_one_upload_reaches_rendered_reviewable_clips(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    if not FFMPEG:
        pytest.skip("ffmpeg is not installed")

    media_root = tmp_path / "media"
    settings = Settings(_env_file=None, admin_token=TOKEN, media_root=media_root)
    app = create_app(
        settings=settings,
        probes={"database": lambda: None},
        session_factory=session_factory,
        services=build_services(settings),
    )
    client = TestClient(app)
    queue = _redis_queue()
    handlers = _handlers(media_root)
    relay = OutboxRelay(
        session_factory=session_factory,
        queue=queue,
        clock=lambda: datetime.now(UTC),
        batch_size=50,
        backoff_cap_seconds=300,
    )

    payload = tmp_path / "input.mp4"
    _make_mp4(payload)
    with payload.open("rb") as media:
        response = client.post(
            "/api/v1/sources/upload",
            files={"file": ("episode.mp4", media, "video/mp4")},
            data={"auto_process": "true"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 202
    source_id = UUID(response.json()["source"]["id"])

    _drain(relay, session_factory, queue, handlers)

    storage = MediaStorage(media_root)
    with session_factory() as session:
        source = SourceRepository(session).get(source_id)
        assert source is not None and source.status is SourceStatus.READY
        transcript = TranscriptRepository(session).get_document(source_id)
        assert transcript is not None
        clips = ClipRepository(session).list_for_source(source_id)
        assert clips
        for clip in clips:
            assert clip.status is ClipStatus.READY_FOR_REVIEW
            assert clip.render_width == 1080 and clip.render_height == 1920
            assert clip.output_path is not None
            assert storage.resolve(clip.output_path).is_file()

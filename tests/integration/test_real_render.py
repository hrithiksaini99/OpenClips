"""End-to-end render test producing a real 9:16 artifact verified with FFprobe."""

import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from openclips.application.rendering import RenderCoordinator
from openclips.domain.captions import get_template
from openclips.domain.clips import ClipEvent
from openclips.domain.sources import SourceEvent, SourceKind
from openclips.domain.transcripts import TranscriptDocument, TranscriptSegment, TranscriptWord
from openclips.infrastructure.media_storage import MediaStorage, read_file_chunks
from openclips.infrastructure.repositories import (
    ClipRepository,
    JobRepository,
    SourceRepository,
    TranscriptRepository,
)
from openclips.providers.captions import CaptionEdit
from openclips.providers.renderer import FFmpegRenderer, probe_media

pytestmark = pytest.mark.integration

FFMPEG = shutil.which("ffmpeg")




def _make_tiny_mp4(destination: Path) -> None:
    subprocess.run(
        [
            str(FFMPEG),
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:size=320x180:duration=25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:duration=25",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-y",
            str(destination),
        ],
        check=True,
        capture_output=True,
    )


def test_render_produces_playable_vertical_media(session: Session, tmp_path: Path) -> None:
    if not FFMPEG:
        pytest.skip("ffmpeg is not installed")

    media_root = tmp_path / "media"
    storage = MediaStorage(media_root)
    payload = tmp_path / "input.mp4"
    _make_tiny_mp4(payload)

    sources = SourceRepository(session)
    record = sources.create(
        source_kind=SourceKind.LOCAL_UPLOAD,
        original_locator="show.mp4",
        external_id=None,
        idempotency_key="render-e2e-key",
        display_name="show.mp4",
        retain_until=datetime.now(UTC) + timedelta(days=7),
    )
    sources.transition(record.id, SourceEvent.START)
    source = sources.attach_media(record.id, media_path="local_upload/show.mp4", byte_size=1)
    storage.write_stream("local_upload/show.mp4", read_file_chunks(payload))

    words = tuple(
        TranscriptWord(text=text, start=start, end=start + 0.8, probability=0.95)
        for start, text in [
            (0.5, "The"),
            (1.5, "secret"),
            (2.5, "is"),
            (3.5, "persistence"),
        ]
    )
    segment = TranscriptSegment(
        start=0.0, end=24.0, text="The secret is persistence", words=words
    )
    document = TranscriptDocument(language="en", duration=24.0, segments=(segment,))
    transcripts = TranscriptRepository(session)
    transcripts.upsert_for_source(source.id, document)

    clips = ClipRepository(session)
    clip = clips.create(
        source_asset_id=source.id,
        title="Persistence pays off big",
        start_time=0.0,
        end_time=24.0,
        selection_score=2.0,
    )
    clips.transition(clip.id, ClipEvent.READY)
    session.commit()

    coordinator = RenderCoordinator(
        clips=clips,
        sources=sources,
        transcripts=transcripts,
        jobs=JobRepository(session),
        renderer=FFmpegRenderer(),
        storage=storage,
        style=get_template("karaoke"),
        edits=(CaptionEdit("secret", "truth"),),
        mask_words=frozenset(),
    )

    rendered = coordinator.render_clip(clip.id)

    info = probe_media(storage.root / str(rendered.output_path))
    assert info.has_video and info.has_audio
    assert info.width == 1080
    assert info.height == 1920
    caption_text = (storage.root / str(rendered.caption_path)).read_text()
    assert "TRUTH" in caption_text

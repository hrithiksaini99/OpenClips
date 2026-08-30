"""Shared isolation for the studio tests.

Every studio module reads and writes real directories inside the project, so a
test that forgot to redirect them would touch the actual publish queue or delete
the actual clips. These fixtures point all of it at a temporary directory.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from studio import pipeline, publisher
from studio.metadata import PostMetadata, compose_description


@pytest.fixture
def studio_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(pipeline, "CLIPS_DIR", tmp_path / "clips")
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(publisher, "STATE_FILE", tmp_path / "clips" / "publish.json")
    return tmp_path


@pytest.fixture
def connected(monkeypatch: pytest.MonkeyPatch) -> None:
    """An attached account, so the queue is willing to act."""
    monkeypatch.setattr(publisher.youtube, "connected", lambda: True)


class StubWriter:
    """Stands in for Gemma, so the tests need no model running."""

    def write(self, *, text: str, fallback_title: str, source_title: str = "",
              source_url: str = "") -> PostMetadata:
        return PostMetadata(
            title=fallback_title,
            description=compose_description(
                text, hashtags=("#clip",), source_title=source_title, source_url=source_url
            ),
            hashtags=("#clip",),
        )


@pytest.fixture
def make_job() -> Callable[..., pipeline.JobState]:
    """Build a finished job with rendered clips and real files on disk."""

    def build(job_id: str = "job", clips: int = 3,
              source: str = "https://youtu.be/abcdefghijk",
              *, poster: bool = False) -> pipeline.JobState:
        state = pipeline.JobState(id=job_id, source=source, title="Episode", status="done")
        state.clips = [
            pipeline.ClipRecord(
                id=f"{index:02d}-clip", title=f"Clip {index}", start=index * 60.0,
                end=index * 60.0 + 40, duration=40.0, score=90.0,
                text=f"Transcript {index}.", file=f"{index:02d}-clip.mp4",
                thumbnail=f"{index:02d}-clip.jpg",
                poster=f"{index:02d}-clip-yt.jpg" if poster else "",
            )
            for index in range(1, clips + 1)
        ]
        pipeline.write_state(state)
        directory = pipeline.job_dir(job_id)
        for clip in state.clips:
            (directory / clip.file).write_bytes(b"\0" * 4096)
            (directory / clip.thumbnail).write_bytes(b"\0" * 256)
            if clip.poster:
                (directory / clip.poster).write_bytes(b"\0" * 512)
        return state

    return build


@pytest.fixture
def queued(make_job: Callable[..., pipeline.JobState]) -> Callable[..., list[publisher.Entry]]:
    """A job whose clips have been written up and put in the queue."""

    def build(clips: int = 3, **kwargs: object) -> list[publisher.Entry]:
        make_job(clips=clips, **kwargs)
        return publisher.enqueue("job", writer=StubWriter())

    return build

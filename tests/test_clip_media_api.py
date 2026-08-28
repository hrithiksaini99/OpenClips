"""Contract tests for the public clip media and caption endpoints."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from openclips.config import Settings
from openclips.domain.clips import ClipEvent
from openclips.infrastructure.models import Base
from openclips.infrastructure.repositories import ClipRepository
from openclips.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> Iterator[tuple[TestClient, sessionmaker, Settings]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    settings = Settings(_env_file=None, media_root=tmp_path / "media", admin_token="t")
    app = create_app(
        settings=settings, probes={"database": lambda: None}, session_factory=factory
    )
    yield TestClient(app), factory, settings
    engine.dispose()


def _make_clip(factory: sessionmaker, *, rendered: bool, media_root: Path) -> str:
    with factory() as session:
        clips = ClipRepository(session)
        clip = clips.create(source_asset_id=None, title="Clip", start_time=0.0, end_time=5.0)
        clips.transition(clip.id, ClipEvent.READY)
        if rendered:
            output_key = f"clips/{clip.id}/render.mp4"
            caption_key = f"clips/{clip.id}/captions.ass"
            (media_root / "clips" / str(clip.id)).mkdir(parents=True, exist_ok=True)
            (media_root / output_key).write_bytes(b"video-bytes")
            (media_root / caption_key).write_text("caption-text")
            clip.output_path = output_key
            clip.caption_path = caption_key
        session.commit()
        return str(clip.id)


def test_media_endpoint_streams_rendered_clip(client) -> None:
    test_client, factory, settings = client
    clip_id = _make_clip(factory, rendered=True, media_root=settings.media_root)

    response = test_client.get(f"/api/v1/clips/{clip_id}/media")

    assert response.status_code == 200
    assert response.content == b"video-bytes"


def test_caption_endpoint_streams_rendered_clip(client) -> None:
    test_client, factory, settings = client
    clip_id = _make_clip(factory, rendered=True, media_root=settings.media_root)

    response = test_client.get(f"/api/v1/clips/{clip_id}/caption")

    assert response.status_code == 200
    assert response.content == b"caption-text"


def test_media_endpoint_404_for_unrendered_clip(client) -> None:
    test_client, factory, settings = client
    clip_id = _make_clip(factory, rendered=False, media_root=settings.media_root)

    assert test_client.get(f"/api/v1/clips/{clip_id}/media").status_code == 404
    assert test_client.get(f"/api/v1/clips/{clip_id}/caption").status_code == 404

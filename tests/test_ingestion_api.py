"""Authenticated upload and YouTube ingestion HTTP endpoints."""

import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from openclips.application.services import build_services
from openclips.config import Settings
from openclips.domain.sources import SourceKind, SourceStatus
from openclips.infrastructure.models import Base, JobRecord, SourceAssetRecord
from openclips.main import create_app
from openclips.providers.youtube import YtDlpDownloader

TOKEN = "test-admin-token"


def _settings(admin_token: str | None, tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {"media_root": tmp_path / "media"}
    if admin_token is not None:
        values["admin_token"] = admin_token
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _client(settings: Settings) -> tuple[TestClient, sessionmaker]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    services = build_services(settings)
    app = create_app(
        settings=settings,
        probes={"database": lambda: None},
        session_factory=factory,
        services=services,
    )
    return TestClient(app), factory


@pytest.fixture
def client(tmp_path: Path) -> Iterator[tuple[TestClient, sessionmaker]]:
    test_client, factory = _client(_settings(TOKEN, tmp_path))
    yield test_client, factory


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_upload_streams_source_and_enqueues_transcription(client) -> None:
    test_client, factory = client

    response = test_client.post(
        "/api/v1/sources/upload",
        files={"file": ("episode.mp4", io.BytesIO(b"tiny-media-bytes"), "video/mp4")},
        data={"auto_process": "true"},
        headers=_auth(),
    )

    assert response.status_code == 202
    body = response.json()
    assert body["source"]["status"] == SourceStatus.READY.value
    assert body["next_job"]["kind"] == "transcribe"
    with factory() as session:
        assert session.query(SourceAssetRecord).count() == 1
        assert session.query(JobRecord).filter_by(kind="transcribe").count() == 1


def test_upload_without_auto_process_creates_no_job(client) -> None:
    test_client, factory = client

    response = test_client.post(
        "/api/v1/sources/upload",
        files={"file": ("episode.mp4", io.BytesIO(b"tiny-media-bytes"), "video/mp4")},
        data={"auto_process": "false"},
        headers=_auth(),
    )

    assert response.status_code == 202
    assert response.json()["next_job"] is None
    with factory() as session:
        source = session.query(SourceAssetRecord).one()
        assert source.auto_process is False
        assert session.query(JobRecord).count() == 0


def test_upload_rejects_unsupported_extension(client) -> None:
    test_client, _ = client

    response = test_client.post(
        "/api/v1/sources/upload",
        files={"file": ("notes.txt", io.BytesIO(b"nope"), "text/plain")},
        headers=_auth(),
    )

    assert response.status_code == 409


def test_upload_enforces_max_upload_bytes(tmp_path: Path) -> None:
    settings = _settings(TOKEN, tmp_path, max_upload_bytes=8)
    test_client, factory = _client(settings)

    response = test_client.post(
        "/api/v1/sources/upload",
        files={"file": ("episode.mp4", io.BytesIO(b"this-is-way-too-large"), "video/mp4")},
        headers=_auth(),
    )

    assert response.status_code == 413
    with factory() as session:
        assert session.query(SourceAssetRecord).count() == 0


def test_upload_requires_admin(client) -> None:
    test_client, _ = client

    response = test_client.post(
        "/api/v1/sources/upload",
        files={"file": ("episode.mp4", io.BytesIO(b"tiny"), "video/mp4")},
    )

    assert response.status_code == 401


def test_youtube_returns_before_background_download(client) -> None:
    test_client, factory = client

    response = test_client.post(
        "/api/v1/sources/youtube",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "auto_process": True},
        headers=_auth(),
    )

    assert response.status_code == 202
    body = response.json()
    assert body["source"]["status"] == SourceStatus.PENDING.value
    assert body["source"]["source_kind"] == SourceKind.YOUTUBE_VIDEO.value
    assert body["next_job"]["kind"] == "ingest_youtube"
    with factory() as session:
        assert session.query(JobRecord).filter_by(kind="ingest_youtube").count() == 1


def test_youtube_rejects_unsupported_url(client) -> None:
    test_client, _ = client

    response = test_client.post(
        "/api/v1/sources/youtube",
        json={"url": "https://example.com/watch?v=abc", "auto_process": True},
        headers=_auth(),
    )

    assert response.status_code == 422


def test_youtube_requires_admin(client) -> None:
    test_client, _ = client

    response = test_client.post(
        "/api/v1/sources/youtube",
        json={"url": "https://youtu.be/dQw4w9WgXcQ"},
    )

    assert response.status_code == 401


def test_services_expose_downloader_and_upload_limit(tmp_path: Path) -> None:
    services = build_services(_settings(TOKEN, tmp_path, max_upload_bytes=1234))
    assert isinstance(services.downloader, YtDlpDownloader)
    assert services.max_upload_bytes == 1234

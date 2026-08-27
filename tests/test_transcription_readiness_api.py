"""Public, non-gating transcription-readiness endpoint."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from openclips.application.services import build_services
from openclips.config import Settings
from openclips.infrastructure.models import Base
from openclips.main import create_app


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        admin_token="test-admin-token",
        media_root=tmp_path / "media",
        model_cache_root=tmp_path / "cache",
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[tuple[TestClient, Settings]]:
    settings = _settings(tmp_path)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    app = create_app(
        settings=settings,
        probes={"database": lambda: None},
        session_factory=factory,
        services=build_services(settings),
    )
    yield TestClient(app), settings


def test_readiness_endpoint_is_public_and_non_gating(client) -> None:
    test_client, _ = client

    response = test_client.get("/api/v1/system/transcription-readiness")

    assert response.status_code == 200
    assert response.json()["status"] in {"missing", "downloading", "available"}


def test_readiness_endpoint_reports_missing_by_default(client) -> None:
    test_client, _ = client

    response = test_client.get("/api/v1/system/transcription-readiness")

    assert response.json()["status"] == "missing"


def test_readiness_endpoint_reports_downloading_when_marker_present(client) -> None:
    test_client, settings = client
    marker = settings.model_cache_root / ".openclips-downloading-base"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()

    response = test_client.get("/api/v1/system/transcription-readiness")

    assert response.json()["status"] == "downloading"


def test_ready_endpoint_stays_database_and_redis_only(client) -> None:
    test_client, _ = client
    # Readiness of the model must not leak into the deployment /ready probe.
    response = test_client.get("/ready")

    assert response.status_code == 200
    assert "transcription" not in response.json()["checks"]

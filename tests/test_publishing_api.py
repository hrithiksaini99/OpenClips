"""Contract tests for the working V1 publication endpoints."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from openclips.config import Settings
from openclips.domain.clips import ClipEvent
from openclips.domain.publishing import (
    Platform,
    PublicationEvent,
    PublicationStatus,
)
from openclips.domain.sources import SourceEvent, SourceKind
from openclips.infrastructure.models import Base
from openclips.infrastructure.repositories import (
    ClipRepository,
    PublicationRepository,
    SourceRepository,
)
from openclips.main import create_app

TOKEN = "test-admin-token"


def _settings() -> Settings:
    return Settings(_env_file=None, media_root=Path("/tmp/oc-pub-api-media"), admin_token=TOKEN)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[tuple[TestClient, sessionmaker]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    settings = _settings()
    settings.media_root = tmp_path / "media"
    app = create_app(
        settings=settings,
        probes={"database": lambda: None},
        session_factory=factory,
    )
    yield TestClient(app), factory
    engine.dispose()


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_clips(factory: sessionmaker, *, approve: int = 2, ready: int = 0) -> list[str]:
    with factory() as session:
        sources = SourceRepository(session)
        record = sources.create(
            source_kind=SourceKind.LOCAL_UPLOAD,
            original_locator="show.mp4",
            external_id=None,
            idempotency_key=f"pub-api-{datetime.now(UTC).timestamp()}",
            display_name="show.mp4",
            retain_until=datetime.now(UTC) + timedelta(days=7),
        )
        sources.transition(record.id, SourceEvent.START)
        asset = sources.attach_media(record.id, media_path="local/show.mp4", byte_size=10)
        clips = ClipRepository(session)
        ids: list[str] = []
        for index in range(approve + ready):
            clip = clips.create(
                source_asset_id=asset.id,
                title=f"Clip {index}",
                start_time=0.0,
                end_time=30.0,
            )
            clips.transition(clip.id, ClipEvent.READY)
            if index < approve:
                clips.transition(clip.id, ClipEvent.APPROVE)
            ids.append(str(clip.id))
        session.commit()
        return ids


def _make_publication(
    factory: sessionmaker,
    clip_id: str,
    *,
    events: tuple[PublicationEvent, ...] = (),
    attempts: int | None = None,
) -> str:
    with factory() as session:
        publications = PublicationRepository(session)
        record = publications.create(
            clip_id=UUID(clip_id),
            platform=Platform.YOUTUBE_SHORTS,
            scheduled_at=datetime.now(UTC),
        )
        for event in events:
            publications.transition(record.id, event)
        if attempts is not None:
            record.attempts = attempts
        session.commit()
        return str(record.id)


def test_unauthenticated_schedule_is_401(client) -> None:
    test_client, factory = client
    clip_id = _seed_clips(factory)[0]

    response = test_client.post(
        "/api/v1/publications",
        json={"clip_id": clip_id, "platform": "YOUTUBE_SHORTS"},
    )

    assert response.status_code == 401


def test_schedule_unknown_clip_is_404(client) -> None:
    test_client, _ = client

    response = test_client.post(
        "/api/v1/publications",
        json={
            "clip_id": "00000000-0000-0000-0000-000000000000",
            "platform": "YOUTUBE_SHORTS",
        },
        headers=_auth(),
    )

    assert response.status_code == 404


def test_schedule_non_approved_clip_is_409(client) -> None:
    test_client, factory = client
    ready_id = _seed_clips(factory, approve=0, ready=1)[0]

    response = test_client.post(
        "/api/v1/publications",
        json={"clip_id": ready_id, "platform": "YOUTUBE_SHORTS"},
        headers=_auth(),
    )

    assert response.status_code == 409


def test_schedule_youtube_immediately_and_preserves_explicit_timestamp(client) -> None:
    test_client, factory = client
    approved = _seed_clips(factory)

    immediate = test_client.post(
        "/api/v1/publications",
        json={"clip_id": approved[0], "platform": "YOUTUBE_SHORTS"},
        headers=_auth(),
    )
    assert immediate.status_code == 201
    body = immediate.json()
    assert body["status"] == PublicationStatus.SCHEDULED.value
    assert body["platform"] == Platform.YOUTUBE_SHORTS.value
    assert body["clip_id"] == approved[0]

    when = datetime(2027, 1, 2, 3, 4, 5, tzinfo=UTC)
    explicit = test_client.post(
        "/api/v1/publications",
        json={
            "clip_id": approved[1],
            "platform": "YOUTUBE_SHORTS",
            "scheduled_at": when.isoformat(),
        },
        headers=_auth(),
    )
    assert explicit.status_code == 201
    assert datetime.fromisoformat(explicit.json()["scheduled_at"]) == when


def test_schedule_instagram_without_media_provider_is_409_and_creates_no_row(client) -> None:
    test_client, factory = client
    approved = _seed_clips(factory)

    response = test_client.post(
        "/api/v1/publications",
        json={"clip_id": approved[0], "platform": "INSTAGRAM_REELS"},
        headers=_auth(),
    )

    assert response.status_code == 409
    assert "OPENCLIPS_PUBLIC_MEDIA_BASE_URL" in response.json()["detail"]
    with factory() as session:
        assert PublicationRepository(session).list_all() == []


def test_invalid_platform_body_is_422(client) -> None:
    test_client, factory = client
    approved = _seed_clips(factory)

    response = test_client.post(
        "/api/v1/publications",
        json={"clip_id": approved[0], "platform": "TIKTOK"},
        headers=_auth(),
    )

    assert response.status_code == 422


def test_invalid_scheduled_at_body_is_422(client) -> None:
    test_client, factory = client
    approved = _seed_clips(factory)

    response = test_client.post(
        "/api/v1/publications",
        json={
            "clip_id": approved[0],
            "platform": "YOUTUBE_SHORTS",
            "scheduled_at": "not-a-timestamp",
        },
        headers=_auth(),
    )

    assert response.status_code == 422


def test_bulk_schedule_returns_one_item_per_clip_with_mixed_results(client) -> None:
    test_client, factory = client
    approved = _seed_clips(factory, approve=1, ready=1)
    unknown = "00000000-0000-0000-0000-000000000009"
    clip_ids = [approved[0], unknown, approved[1]]

    response = test_client.post(
        "/api/v1/publications/bulk",
        json={"clip_ids": clip_ids, "platform": "YOUTUBE_SHORTS"},
        headers=_auth(),
    )

    assert response.status_code == 200
    items = response.json()
    assert [item["clip_id"] for item in items] == clip_ids
    by_id = {item["clip_id"]: item for item in items}
    assert by_id[approved[0]]["ok"] is True
    assert by_id[approved[0]]["publication_id"] is not None
    assert by_id[unknown]["ok"] is False
    assert by_id[unknown]["error"]
    assert by_id[approved[1]]["ok"] is False


def test_public_list_and_get_filters_and_404(client) -> None:
    test_client, factory = client
    approved = _seed_clips(factory, approve=2)
    yt = test_client.post(
        "/api/v1/publications",
        json={"clip_id": approved[0], "platform": "YOUTUBE_SHORTS"},
        headers=_auth(),
    ).json()
    test_client.post(
        "/api/v1/publications",
        json={"clip_id": approved[1], "platform": "YOUTUBE_SHORTS"},
        headers=_auth(),
    )

    listing = test_client.get("/api/v1/publications")
    assert listing.status_code == 200
    assert len(listing.json()) == 2

    filtered = test_client.get(
        "/api/v1/publications", params={"platform": "INSTAGRAM_REELS"}
    )
    assert filtered.status_code == 200
    assert filtered.json() == []

    by_status = test_client.get(
        "/api/v1/publications", params={"publication_status": "SCHEDULED", "limit": 1}
    )
    assert by_status.status_code == 200
    assert len(by_status.json()) == 1

    detail = test_client.get(f"/api/v1/publications/{yt['id']}")
    assert detail.status_code == 200
    assert detail.json()["id"] == yt["id"]

    missing = test_client.get(
        "/api/v1/publications/00000000-0000-0000-0000-000000000000"
    )
    assert missing.status_code == 404


def test_retry_failed_publication_returns_scheduled(client) -> None:
    test_client, factory = client
    clip_id = _seed_clips(factory, approve=1)[0]
    publication_id = _make_publication(
        factory,
        clip_id,
        events=(
            PublicationEvent.ENQUEUE,
            PublicationEvent.START,
            PublicationEvent.FAIL,
        ),
    )

    response = test_client.post(
        f"/api/v1/publications/{publication_id}/retry", headers=_auth()
    )

    assert response.status_code == 200
    assert response.json()["status"] == PublicationStatus.SCHEDULED.value


def test_retry_exhausted_publication_is_409(client) -> None:
    test_client, factory = client
    clip_id = _seed_clips(factory, approve=1)[0]
    publication_id = _make_publication(
        factory,
        clip_id,
        events=(
            PublicationEvent.ENQUEUE,
            PublicationEvent.START,
            PublicationEvent.FAIL,
        ),
        attempts=5,
    )

    response = test_client.post(
        f"/api/v1/publications/{publication_id}/retry", headers=_auth()
    )

    assert response.status_code == 409


def test_retry_unknown_publication_is_404(client) -> None:
    test_client, _ = client

    response = test_client.post(
        "/api/v1/publications/00000000-0000-0000-0000-000000000000/retry",
        headers=_auth(),
    )

    assert response.status_code == 404


def test_cancel_scheduled_and_queued_publications(client) -> None:
    test_client, factory = client
    clip_ids = _seed_clips(factory, approve=2)
    scheduled = _make_publication(factory, clip_ids[0])
    queued = _make_publication(factory, clip_ids[1], events=(PublicationEvent.ENQUEUE,))

    for publication_id in (scheduled, queued):
        response = test_client.post(
            f"/api/v1/publications/{publication_id}/cancel", headers=_auth()
        )
        assert response.status_code == 200
        assert response.json()["status"] == PublicationStatus.CANCELLED.value


def test_invalid_cancellation_is_409(client) -> None:
    test_client, factory = client
    clip_id = _seed_clips(factory, approve=1)[0]
    published = _make_publication(
        factory,
        clip_id,
        events=(PublicationEvent.ENQUEUE, PublicationEvent.START),
    )
    with factory() as session:
        PublicationRepository(session).attach_result(
            UUID(published), external_id="ext", external_url="https://x/ext"
        )
        session.commit()

    response = test_client.post(
        f"/api/v1/publications/{published}/cancel", headers=_auth()
    )

    assert response.status_code == 409


def test_unauthenticated_retry_and_cancel_are_401(client) -> None:
    test_client, factory = client
    clip_id = _seed_clips(factory, approve=1)[0]
    publication_id = _make_publication(factory, clip_id)

    assert (
        test_client.post(f"/api/v1/publications/{publication_id}/retry").status_code == 401
    )
    assert (
        test_client.post(f"/api/v1/publications/{publication_id}/cancel").status_code
        == 401
    )

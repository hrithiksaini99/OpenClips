"""Contract tests for the OpenClips review API."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from openclips.config import Settings
from openclips.domain.clips import ClipEvent, ClipStatus
from openclips.domain.sources import SourceEvent, SourceKind
from openclips.infrastructure.models import Base
from openclips.infrastructure.repositories import ClipRepository, SourceRepository
from openclips.main import create_app

TOKEN = "test-admin-token"


def _settings(admin_token: str | None) -> Settings:
    overrides: dict[str, str] = {}
    if admin_token is not None:
        overrides["admin_token"] = admin_token
    return Settings(_env_file=None, media_root=Path("/tmp/oc-api-media"), **overrides)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[tuple[TestClient, sessionmaker]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    settings = _settings(TOKEN)
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


def _seed(factory: sessionmaker) -> dict[str, object]:
    """Create one ready source and two reviewable clips; return their ids."""
    with factory() as session:
        sources = SourceRepository(session)
        record = sources.create(
            source_kind=SourceKind.LOCAL_UPLOAD,
            original_locator="show.mp4",
            external_id=None,
            idempotency_key=f"api-{datetime.now(UTC).timestamp()}",
            display_name="show.mp4",
            retain_until=datetime.now(UTC) + timedelta(days=7),
        )
        sources.transition(record.id, SourceEvent.START)
        ready = sources.attach_media(
            record.id, media_path="local_upload/show.mp4", byte_size=10
        )
        clips = ClipRepository(session)
        created_ids = []
        for title, score in (("Alpha clip", 2.5), ("Beta clip", 1.5)):
            clip = clips.create(
                source_asset_id=ready.id,
                title=title,
                start_time=0.0,
                end_time=30.0,
                selection_score=score,
            )
            clips.transition(clip.id, ClipEvent.READY)
            created_ids.append(clip.id)
        pending = sources.create(
            source_kind=SourceKind.LOCAL_UPLOAD,
            original_locator="pending.mp4",
            external_id=None,
            idempotency_key=f"api-pending-{datetime.now(UTC).timestamp()}",
            display_name="pending.mp4",
            retain_until=datetime.now(UTC) + timedelta(days=7),
        )
        session.commit()
        return {
            "source_id": str(ready.id),
            "pending_source_id": str(pending.id),
            "clip1": str(created_ids[0]),
            "clip2": str(created_ids[1]),
        }


def test_mutations_reject_missing_or_wrong_credentials(client) -> None:
    test_client, factory = client
    ids = _seed(factory)

    assert (
        test_client.post(f"/api/v1/clips/{ids['clip1']}/approve").status_code == 401
    )
    response = test_client.post(
        f"/api/v1/clips/{ids['clip1']}/approve",
        headers=_auth("wrong-token"),
    )

    assert response.status_code == 401


def test_unconfigured_admin_token_fails_closed(tmp_path: Path) -> None:
    del tmp_path
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    app = create_app(
        settings=_settings(None),
        probes={"database": lambda: None},
        session_factory=factory,
    )
    test_client = TestClient(app)

    response = test_client.post(
        "/api/v1/clips/00000000-0000-0000-0000-000000000000/approve"
    )

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]
    engine.dispose()


def test_reads_are_public_but_lifecycle_reads_are_stable(client) -> None:
    test_client, factory = client
    ids = _seed(factory)

    listing = test_client.get("/api/v1/sources")
    detail = test_client.get(f"/api/v1/sources/{ids['source_id']}")
    missing = test_client.get(
        "/api/v1/sources/00000000-0000-0000-0000-000000000000"
    )

    assert listing.status_code == 200
    assert len(listing.json()) == 2
    assert detail.json()["status"] == "READY"
    assert missing.status_code == 404


def test_review_queue_filters_by_status(client) -> None:
    test_client, factory = client
    ids = _seed(factory)

    all_clips = test_client.get("/api/v1/clips").json()
    filtered = test_client.get("/api/v1/clips", params={"review_status": "READY_FOR_REVIEW"})
    empty = test_client.get("/api/v1/clips", params={"review_status": "PUBLISHED"})
    bad = test_client.get("/api/v1/clips", params={"review_status": "NOPE"})

    assert [clip["id"] for clip in all_clips] == [ids["clip1"], ids["clip2"]]
    assert filtered.status_code == 200 and len(filtered.json()) == 2
    assert empty.json() == []
    assert bad.status_code == 409


def test_edit_updates_title_and_moves_to_needs_review(client) -> None:
    test_client, factory = client
    ids = _seed(factory)

    response = test_client.patch(
        f"/api/v1/clips/{ids['clip1']}",
        json={"title": "Renamed clip"},
        headers=_auth(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Renamed clip"
    assert body["status"] == ClipStatus.NEEDS_REVIEW.value


def test_caption_edits_persist_and_transition(client) -> None:
    test_client, factory = client
    ids = _seed(factory)

    response = test_client.put(
        f"/api/v1/clips/{ids['clip1']}/caption-edits",
        json={"edits": [{"match": "damn", "replacement": "brilliant"}]},
        headers=_auth(),
    )
    follow_up = test_client.get(f"/api/v1/clips/{ids['clip1']}")

    assert response.status_code == 200
    assert response.json()["status"] == ClipStatus.NEEDS_REVIEW.value
    assert follow_up.json()["caption_edits"] == [
        {"match": "damn", "replacement": "brilliant"}
    ]


def test_approve_then_reapprove_is_conflict(client) -> None:
    test_client, factory = client
    ids = _seed(factory)

    approved = test_client.post(
        f"/api/v1/clips/{ids['clip1']}/approve", headers=_auth()
    )
    again = test_client.post(f"/api/v1/clips/{ids['clip1']}/approve", headers=_auth())

    assert approved.status_code == 200
    assert approved.json()["status"] == ClipStatus.APPROVED.value
    assert again.status_code == 409


def test_bulk_action_reports_per_item_results(client) -> None:
    test_client, factory = client
    ids = _seed(factory)
    unknown = "00000000-0000-0000-0000-000000000001"

    response = test_client.post(
        "/api/v1/clips/bulk",
        json={
            "action": "reject",
            "clip_ids": [ids["clip1"], unknown, ids["clip2"]],
        },
        headers=_auth(),
    )
    results = {item["clip_id"]: item for item in response.json()}

    assert response.status_code == 200
    assert results[ids["clip1"]]["ok"] is True
    assert results[unknown]["ok"] is False
    assert results[ids["clip2"]]["ok"] is True


def test_transcribe_enqueue_validates_source_state(client) -> None:
    test_client, factory = client
    ids = _seed(factory)

    missing = test_client.post(
        "/api/v1/sources/00000000-0000-0000-0000-000000000002/transcribe",
        headers=_auth(),
    )
    conflict = test_client.post(
        f"/api/v1/sources/{ids['pending_source_id']}/transcribe",
        headers=_auth(),
    )

    assert missing.status_code == 404
    assert conflict.status_code == 409
    assert "not ready" in conflict.json()["detail"]


def test_render_enqueue_requires_reviewable_clip(client) -> None:
    test_client, factory = client
    ids = _seed(factory)

    response = test_client.post(
        f"/api/v1/clips/{ids['clip1']}/render", headers=_auth()
    )

    assert response.status_code == 200
    assert response.json()["kind"] == "render_clip"
    assert response.json()["status"] == "QUEUED"


def test_dashboard_lists_review_queue_titles(client) -> None:
    test_client, factory = client
    _seed(factory)

    response = test_client.get("/api/v1/dashboard")

    assert response.status_code == 200
    assert "Review queue" in response.text
    assert "Alpha clip" in response.text

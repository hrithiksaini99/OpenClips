from collections.abc import Callable

from fastapi.testclient import TestClient

from openclips.config import Settings
from openclips.main import create_app

Probe = Callable[[], None]


def make_client(
    probes: dict[str, Probe] | None = None,
    settings: Settings | None = None,
) -> TestClient:
    return TestClient(
        create_app(settings=settings, probes=probes), raise_server_exceptions=False
    )


def test_health_reports_process_liveness() -> None:
    client = make_client()
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_ok_when_all_dependencies_are_available() -> None:
    client = make_client(probes={"database": lambda: None, "redis": lambda: None})

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok", "redis": "ok"},
    }


def test_ready_reports_each_dependency_independently() -> None:
    def broken_database() -> None:
        msg = "connection refused"
        raise RuntimeError(msg)

    client = make_client(probes={"database": broken_database, "redis": lambda: None})

    response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"].startswith("unavailable: connection refused")
    assert body["checks"]["redis"] == "ok"


def test_ready_defaults_to_real_probes_without_injection() -> None:
    client = make_client()

    response = client.get("/ready")

    assert response.status_code in {200, 503}
    checks = response.json()["checks"]
    assert set(checks) == {"database", "redis"}

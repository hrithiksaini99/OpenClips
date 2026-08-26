import pytest
from pydantic import ValidationError

from openclips.config import Settings


def test_settings_have_deterministic_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.redis_url.startswith("redis://")
    assert settings.worker_concurrency >= 1


def test_settings_parse_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCLIPS_DATABASE_URL", "postgresql+psycopg://u:p@db:5432/x")
    monkeypatch.setenv("OPENCLIPS_REDIS_URL", "redis://cache:6379/0")
    monkeypatch.setenv("OPENCLIPS_WORKER_CONCURRENCY", "4")
    monkeypatch.setenv("OPENCLIPS_MEDIA_ROOT", "/data/media")

    settings = Settings(_env_file=None)

    assert settings.database_url == "postgresql+psycopg://u:p@db:5432/x"
    assert settings.redis_url == "redis://cache:6379/0"
    assert settings.worker_concurrency == 4
    assert str(settings.media_root) == "/data/media"


def test_settings_reject_invalid_worker_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCLIPS_WORKER_CONCURRENCY", "0")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)

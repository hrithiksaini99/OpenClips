from pathlib import Path

import pytest
from pydantic import ValidationError

from openclips.config import Settings


def test_settings_have_deterministic_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.redis_url.startswith("redis://")
    assert settings.worker_concurrency >= 1
    assert settings.model_cache_root == Path("/root/.cache/huggingface/hub")


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


def test_operational_limits_are_typed() -> None:
    settings = Settings(
        _env_file=None,
        max_upload_bytes=1024,
        outbox_batch_size=7,
        outbox_backoff_cap_seconds=90,
    )

    assert settings.max_upload_bytes == 1024
    assert settings.outbox_batch_size == 7
    assert settings.outbox_backoff_cap_seconds == 90


def test_settings_reject_invalid_worker_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCLIPS_WORKER_CONCURRENCY", "0")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_stage_limit_can_equal_worker_concurrency() -> None:
    settings = Settings(
        _env_file=None,
        worker_concurrency=2,
        max_concurrent_renders=2,
    )

    assert settings.max_concurrent_renders == 2


def test_stage_limit_cannot_exceed_worker_concurrency() -> None:
    with pytest.raises(ValidationError, match="max_concurrent_renders"):
        Settings(
            _env_file=None,
            worker_concurrency=2,
            max_concurrent_renders=3,
        )

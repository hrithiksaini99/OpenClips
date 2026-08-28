from pathlib import Path
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application configuration loaded from the environment."""

    model_config = SettingsConfigDict(
        env_prefix="OPENCLIPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://openclips:openclips@localhost:5432/openclips"
    redis_url: str = "redis://localhost:6379/0"
    media_root: Path = Path("./data/media")
    model_cache_root: Path = Path("/root/.cache/huggingface/hub")
    max_upload_bytes: int = Field(default=10 * 1024 * 1024 * 1024, ge=1)
    worker_concurrency: int = Field(default=2, ge=1)
    max_concurrent_transcriptions: int = Field(default=1, ge=1)
    max_concurrent_renders: int = Field(default=1, ge=1)
    outbox_batch_size: int = Field(default=50, ge=1, le=1000)
    outbox_backoff_cap_seconds: int = Field(default=300, ge=1)
    transcription_model_size: str = "base"
    transcription_device: str = "cpu"
    transcription_compute_type: str = "int8"
    max_clips: int = Field(default=10, ge=3, le=30)
    min_clip_seconds: float = Field(default=20.0, ge=1.0)
    max_clip_seconds: float = Field(default=90.0, ge=1.0)
    caption_template: str = "minimal"
    render_width: int = Field(default=1080, ge=64)
    render_height: int = Field(default=1920, ge=64)
    admin_token: str = ""
    public_media_base_url: str = ""
    instagram_account_id: str = ""
    instagram_access_token: str = ""
    youtube_access_token: str = ""
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8000, ge=1)
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _stage_limits_fit_within_worker_concurrency(self) -> Self:
        for field_name in ("max_concurrent_transcriptions", "max_concurrent_renders"):
            if getattr(self, field_name) > self.worker_concurrency:
                raise ValueError(
                    f"{field_name} ({getattr(self, field_name)}) must not exceed "
                    f"worker_concurrency ({self.worker_concurrency})"
                )
        return self

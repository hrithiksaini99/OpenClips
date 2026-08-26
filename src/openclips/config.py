from pathlib import Path

from pydantic import Field
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
    worker_concurrency: int = Field(default=2, ge=1)
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8000, ge=1)
    log_level: str = "INFO"

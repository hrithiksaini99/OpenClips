from collections.abc import Callable
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.engine import Engine

from openclips.config import Settings

if TYPE_CHECKING:
    from redis import Redis

Probe = Callable[[], None]


def make_database_probe(engine: Engine) -> Probe:
    def probe() -> None:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    return probe


def make_redis_probe(redis_client: "Redis") -> Probe:
    def probe() -> None:
        redis_client.ping()

    return probe


def build_default_probes(settings: Settings) -> dict[str, Probe]:
    """Build readiness probes for each configured dependency.

    Connections are opened lazily so importing this module never requires
    running services.
    """
    import redis

    from openclips.infrastructure.db import make_engine

    return {
        "database": make_database_probe(make_engine(settings.database_url)),
        "redis": make_redis_probe(redis.Redis.from_url(settings.redis_url)),
    }

"""Shared PostgreSQL-backed fixtures for integration tests."""

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from openclips.infrastructure.models import Base


def disposable_database_url(url: str | None) -> str:
    if not url:
        pytest.skip("DATABASE_URL is not configured")
    parsed = make_url(url)
    if not parsed.drivername.startswith("postgresql") or not (
        parsed.database and parsed.database.startswith("openclips_test_")
    ):
        raise pytest.UsageError(
            "DATABASE_URL must target a disposable PostgreSQL database named openclips_test_*"
        )
    return url


def _reset_schema(engine: object) -> None:
    Base.metadata.drop_all(engine)  # type: ignore[arg-type]
    # Leave the database unstamped so the next `alembic upgrade head`
    # rebuilds everything cleanly instead of finding a phantom head.
    with engine.begin() as connection:  # type: ignore[attr-defined]
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))


@pytest.fixture
def session() -> Iterator[Session]:
    url = disposable_database_url(os.getenv("DATABASE_URL"))
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value
        value.rollback()
    _reset_schema(engine)
    engine.dispose()


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A committing session factory over a disposable PostgreSQL database."""
    url = disposable_database_url(os.getenv("DATABASE_URL"))
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    _reset_schema(engine)
    engine.dispose()

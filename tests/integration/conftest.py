"""Shared PostgreSQL-backed fixtures for integration tests."""

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from openclips.infrastructure.models import Base


@pytest.fixture
def session() -> Iterator[Session]:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not configured")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value
        value.rollback()
    Base.metadata.drop_all(engine)
    # Leave the database unstamped so the next `alembic upgrade head`
    # rebuilds everything cleanly instead of finding a phantom head.
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    engine.dispose()

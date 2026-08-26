"""PostgreSQL persistence tests for normalized transcripts."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from openclips.domain.sources import SourceKind
from openclips.domain.transcripts import (
    TranscriptDocument,
    TranscriptSegment,
    TranscriptWord,
)
from openclips.infrastructure.models import Base, SourceAssetRecord
from openclips.infrastructure.repositories import SourceRepository, TranscriptRepository

pytestmark = pytest.mark.integration


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


def _source() -> SourceAssetRecord:
    return SourceAssetRecord(
        source_kind=SourceKind.LOCAL_UPLOAD,
        original_locator="clip.mp4",
        idempotency_key=uuid4().hex,
        display_name="clip.mp4",
        retain_until=datetime.now(UTC) + timedelta(days=7),
    )


def test_transcript_upsert_replaces_and_roundtrips(session: Session) -> None:
    source = _source()
    session.add(source)
    session.flush()
    assert SourceRepository(session).get(source.id) is not None

    word = TranscriptWord(text="hello", start=0.0, end=0.5, probability=0.9)
    segment = TranscriptSegment(start=0.0, end=0.5, text="hello", words=(word,))
    document = TranscriptDocument(language="en", duration=0.5, segments=(segment,))
    transcripts = TranscriptRepository(session)

    first = transcripts.upsert_for_source(source.id, document)
    second = transcripts.upsert_for_source(source.id, document)

    assert first.id == second.id
    restored = transcripts.get_document(source.id)
    assert restored == document


def test_get_document_returns_none_without_transcript(session: Session) -> None:
    source = _source()
    session.add(source)
    session.flush()

    assert TranscriptRepository(session).get_document(source.id) is None

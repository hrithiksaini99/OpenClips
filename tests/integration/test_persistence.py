import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from openclips.domain.clips import ClipEvent
from openclips.domain.jobs import JobEvent
from openclips.infrastructure.models import Base
from openclips.infrastructure.repositories import ClipRepository, JobRepository

pytestmark = pytest.mark.integration


@pytest.fixture
def session():
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not configured")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value
        value.rollback()
    Base.metadata.drop_all(engine)


def test_job_and_clip_repositories_persist_transitions(session):
    job_repo = JobRepository(session)
    job = job_repo.create("ingest")
    job_repo.transition(job.id, JobEvent.START)
    session.commit()
    assert job_repo.get(job.id).status.value == "RUNNING"

    clip_repo = ClipRepository(session)
    clip = clip_repo.create("/data/source.mp4")
    clip_repo.transition(clip.id, ClipEvent.READY)
    session.commit()
    assert clip_repo.get(clip.id).status.value == "READY_FOR_REVIEW"

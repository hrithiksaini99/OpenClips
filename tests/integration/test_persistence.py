
import pytest

from openclips.domain.clips import ClipEvent
from openclips.domain.jobs import JobEvent
from openclips.infrastructure.repositories import ClipRepository, JobRepository

pytestmark = pytest.mark.integration




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

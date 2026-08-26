import pytest

from openclips.domain.errors import InvalidTransitionError
from openclips.domain.jobs import JobEvent, JobStateMachine, JobStatus


def test_job_runs_to_success():
    status = JobStateMachine.transition(JobStatus.QUEUED, JobEvent.START)
    assert JobStateMachine.transition(status, JobEvent.SUCCEED) == JobStatus.SUCCEEDED


def test_failed_job_can_retry():
    assert JobStateMachine.transition(JobStatus.FAILED, JobEvent.RETRY) == JobStatus.QUEUED


def test_job_cannot_succeed_before_start():
    with pytest.raises(InvalidTransitionError, match="Cannot apply SUCCEED"):
        JobStateMachine.transition(JobStatus.QUEUED, JobEvent.SUCCEED)

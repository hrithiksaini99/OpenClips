from enum import StrEnum

from openclips.domain.errors import InvalidTransitionError


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class JobEvent(StrEnum):
    START = "START"
    SUCCEED = "SUCCEED"
    FAIL = "FAIL"
    RETRY = "RETRY"
    RECOVER = "RECOVER"


class JobStateMachine:
    _transitions = {
        (JobStatus.QUEUED, JobEvent.START): JobStatus.RUNNING,
        (JobStatus.RUNNING, JobEvent.SUCCEED): JobStatus.SUCCEEDED,
        (JobStatus.RUNNING, JobEvent.FAIL): JobStatus.FAILED,
        (JobStatus.FAILED, JobEvent.RETRY): JobStatus.QUEUED,
        (JobStatus.RUNNING, JobEvent.RECOVER): JobStatus.QUEUED,
    }

    @classmethod
    def transition(cls, current: JobStatus, event: JobEvent) -> JobStatus:
        try:
            return cls._transitions[(current, event)]
        except KeyError as error:
            raise InvalidTransitionError(f"Cannot apply {event} to job in {current}") from error

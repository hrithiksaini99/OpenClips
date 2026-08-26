from uuid import UUID

from sqlalchemy.orm import Session

from openclips.domain.clips import ClipEvent, ClipStateMachine
from openclips.domain.jobs import JobEvent, JobStateMachine
from openclips.infrastructure.models import ClipRecord, JobRecord


class JobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, kind: str) -> JobRecord:
        record = JobRecord(kind=kind)
        self.session.add(record)
        self.session.flush()
        return record

    def get(self, job_id: UUID) -> JobRecord | None:
        return self.session.get(JobRecord, job_id)

    def transition(self, job_id: UUID, event: JobEvent) -> JobRecord:
        record = self.session.get(JobRecord, job_id)
        if record is None:
            raise KeyError(job_id)
        record.status = JobStateMachine.transition(record.status, event)
        if event in (JobEvent.START, JobEvent.RETRY):
            record.attempts += 1
        self.session.flush()
        return record


class ClipRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, source_path: str) -> ClipRecord:
        record = ClipRecord(source_path=source_path)
        self.session.add(record)
        self.session.flush()
        return record

    def get(self, clip_id: UUID) -> ClipRecord | None:
        return self.session.get(ClipRecord, clip_id)

    def transition(self, clip_id: UUID, event: ClipEvent) -> ClipRecord:
        record = self.session.get(ClipRecord, clip_id)
        if record is None:
            raise KeyError(clip_id)
        record.status = ClipStateMachine.transition(record.status, event)
        self.session.flush()
        return record

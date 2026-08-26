from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from openclips.domain.clips import ClipEvent, ClipStateMachine
from openclips.domain.jobs import JobEvent, JobStateMachine
from openclips.domain.sources import SourceEvent, SourceKind, SourceStateMachine
from openclips.infrastructure.models import ClipRecord, JobRecord, SourceAssetRecord


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


class SourceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        source_kind: SourceKind,
        original_locator: str,
        external_id: str | None,
        idempotency_key: str,
        display_name: str,
        retain_until: datetime,
    ) -> SourceAssetRecord:
        record = SourceAssetRecord(
            source_kind=source_kind,
            original_locator=original_locator,
            external_id=external_id,
            idempotency_key=idempotency_key,
            display_name=display_name,
            retain_until=retain_until,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get(self, source_id: UUID) -> SourceAssetRecord | None:
        return self.session.get(SourceAssetRecord, source_id)

    def get_by_idempotency_key(self, idempotency_key: str) -> SourceAssetRecord | None:
        return (
            self.session.query(SourceAssetRecord)
            .filter(SourceAssetRecord.idempotency_key == idempotency_key)
            .one_or_none()
        )

    def transition(
        self, source_id: UUID, event: SourceEvent, *, error: str | None = None
    ) -> SourceAssetRecord:
        record = self.session.get(SourceAssetRecord, source_id)
        if record is None:
            raise KeyError(source_id)
        record.status = SourceStateMachine.transition(record.status, event)
        if error is not None:
            record.error = error
        self.session.flush()
        return record

    def attach_media(
        self, source_id: UUID, *, media_path: str, byte_size: int
    ) -> SourceAssetRecord:
        record = self.session.get(SourceAssetRecord, source_id)
        if record is None:
            raise KeyError(source_id)
        record.media_path = media_path
        record.byte_size = byte_size
        record.status = SourceStateMachine.transition(record.status, SourceEvent.SUCCEED)
        self.session.flush()
        return record

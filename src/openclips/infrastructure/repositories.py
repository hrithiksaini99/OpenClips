from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from openclips.application.pipeline import queue_for_job_kind
from openclips.domain.clips import ClipEvent, ClipStateMachine, ClipStatus
from openclips.domain.jobs import JobEvent, JobStateMachine, JobStatus
from openclips.domain.outbox import OutboxStatus
from openclips.domain.publishing import (
    Platform,
    PublicationEvent,
    PublicationStateMachine,
    PublicationStatus,
)
from openclips.domain.sources import SourceEvent, SourceKind, SourceStateMachine
from openclips.domain.transcripts import TranscriptDocument, TranscriptSegment, TranscriptWord
from openclips.infrastructure.models import (
    ClipRecord,
    JobRecord,
    OutboxRecord,
    PublicationRecord,
    SourceAssetRecord,
    TranscriptRecord,
)


class JobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, kind: str, *, payload: str | None = None) -> JobRecord:
        record = JobRecord(kind=kind, payload=payload)
        self.session.add(record)
        self.session.flush()
        return record

    def create_dispatched(
        self, kind: str, *, payload: str | None, queue_name: str
    ) -> tuple[JobRecord, OutboxRecord]:
        job = self.create(kind, payload=payload)
        event = OutboxRecord(job_id=job.id, queue_name=queue_name)
        self.session.add(event)
        self.session.flush()
        return job, event

    def get(self, job_id: UUID) -> JobRecord | None:
        return self.session.get(JobRecord, job_id)

    def get_for_update(self, job_id: UUID) -> JobRecord | None:
        return (
            self.session.query(JobRecord)
            .filter(JobRecord.id == job_id)
            .with_for_update()
            .one_or_none()
        )

    def list_all(
        self,
        *,
        status: JobStatus | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[JobRecord]:
        query = self.session.query(JobRecord)
        if status is not None:
            query = query.filter(JobRecord.status == status)
        if kind is not None:
            query = query.filter(JobRecord.kind == kind)
        return query.order_by(JobRecord.created_at.desc(), JobRecord.id).limit(limit).all()

    def transition(
        self, job_id: UUID, event: JobEvent, *, error: str | None = None
    ) -> JobRecord:
        record = self.session.get(JobRecord, job_id)
        if record is None:
            raise KeyError(job_id)
        record.status = JobStateMachine.transition(record.status, event)
        if event in (JobEvent.START, JobEvent.RETRY):
            record.attempts += 1
        if event in (JobEvent.RETRY, JobEvent.RECOVER):
            record.error = None
        if error is not None:
            record.error = error
        self.session.flush()
        return record

    def retry_dispatched(self, job_id: UUID) -> tuple[JobRecord, OutboxRecord]:
        job = self.transition(job_id, JobEvent.RETRY)
        event = OutboxRecord(job_id=job.id, queue_name=queue_for_job_kind(job.kind))
        self.session.add(event)
        self.session.flush()
        return job, event

    def recover_running(self) -> list[JobRecord]:
        records = (
            self.session.query(JobRecord)
            .filter(JobRecord.status == JobStatus.RUNNING)
            .order_by(JobRecord.created_at.asc(), JobRecord.id)
            .with_for_update(skip_locked=True)
            .all()
        )
        for record in records:
            self.transition(record.id, JobEvent.RECOVER)
            self.session.add(
                OutboxRecord(job_id=record.id, queue_name=queue_for_job_kind(record.kind))
            )
        self.session.flush()
        return records


class OutboxRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def due(self, now: datetime, limit: int) -> list[OutboxRecord]:
        return (
            self.session.query(OutboxRecord)
            .filter(
                OutboxRecord.status == OutboxStatus.PENDING,
                OutboxRecord.available_at <= now,
            )
            .order_by(OutboxRecord.available_at.asc(), OutboxRecord.id)
            .with_for_update(skip_locked=True)
            .limit(limit)
            .all()
        )

    def mark_delivered(self, event_id: UUID, delivered_at: datetime) -> OutboxRecord:
        record = self.session.get(OutboxRecord, event_id)
        if record is None:
            raise KeyError(event_id)
        record.status = OutboxStatus.DELIVERED
        record.delivered_at = delivered_at
        self.session.flush()
        return record

    def mark_failed(
        self, event_id: UUID, error: str, next_attempt_at: datetime
    ) -> OutboxRecord:
        record = self.session.get(OutboxRecord, event_id)
        if record is None:
            raise KeyError(event_id)
        record.attempts += 1
        record.last_error = error
        record.available_at = next_attempt_at
        self.session.flush()
        return record


class ClipRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        source_path: str = "",
        *,
        source_asset_id: UUID | None = None,
        title: str | None = None,
        start_time: float | None = None,
        end_time: float | None = None,
        selection_score: float | None = None,
    ) -> ClipRecord:
        record = ClipRecord(
            source_path=source_path,
            source_asset_id=source_asset_id,
            title=title,
            start_time=start_time,
            end_time=end_time,
            selection_score=selection_score,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get(self, clip_id: UUID) -> ClipRecord | None:
        return self.session.get(ClipRecord, clip_id)

    def list_for_source(self, source_asset_id: UUID) -> list[ClipRecord]:
        return (
            self.session.query(ClipRecord)
            .filter(ClipRecord.source_asset_id == source_asset_id)
            .order_by(ClipRecord.start_time.asc(), ClipRecord.id.asc())
            .all()
        )

    def list_all(
        self,
        *,
        status: ClipStatus | None = None,
        limit: int = 100,
    ) -> list[ClipRecord]:
        query = self.session.query(ClipRecord)
        if status is not None:
            query = query.filter(ClipRecord.status == status)
        return (
            query.order_by(ClipRecord.selection_score.desc().nullslast(), ClipRecord.id.asc())
            .limit(limit)
            .all()
        )

    def set_title(self, clip_id: UUID, title: str) -> ClipRecord:
        record = self.session.get(ClipRecord, clip_id)
        if record is None:
            raise KeyError(clip_id)
        record.title = title
        self.session.flush()
        return record

    def set_caption_edits(
        self, clip_id: UUID, edits: list[dict[str, str]]
    ) -> ClipRecord:
        record = self.session.get(ClipRecord, clip_id)
        if record is None:
            raise KeyError(clip_id)
        record.caption_edits = edits
        self.session.flush()
        return record

    def set_timespan(
        self, clip_id: UUID, *, start_time: float | None, end_time: float | None
    ) -> ClipRecord:
        record = self.session.get(ClipRecord, clip_id)
        if record is None:
            raise KeyError(clip_id)
        if start_time is not None:
            record.start_time = start_time
        if end_time is not None:
            record.end_time = end_time
        self.session.flush()
        return record

    def delete_for_source(self, source_asset_id: UUID) -> int:
        records = self.list_for_source(source_asset_id)
        for record in records:
            self.session.delete(record)
        self.session.flush()
        return len(records)

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
        auto_process: bool = True,
    ) -> SourceAssetRecord:
        record = SourceAssetRecord(
            source_kind=source_kind,
            original_locator=original_locator,
            external_id=external_id,
            idempotency_key=idempotency_key,
            display_name=display_name,
            retain_until=retain_until,
            auto_process=auto_process,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get(self, source_id: UUID) -> SourceAssetRecord | None:
        return self.session.get(SourceAssetRecord, source_id)

    def list_all(self, *, limit: int = 100) -> list[SourceAssetRecord]:
        return (
            self.session.query(SourceAssetRecord)
            .order_by(SourceAssetRecord.created_at.desc(), SourceAssetRecord.id)
            .limit(limit)
            .all()
        )

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


def _document_to_payload(document: TranscriptDocument) -> dict[str, Any]:
    return {
        "language": document.language,
        "duration": document.duration,
        "segments": [
            {
                "start": segment.start,
                "end": segment.end,
                "text": segment.text,
                "words": [
                    {
                        "text": word.text,
                        "start": word.start,
                        "end": word.end,
                        "probability": word.probability,
                    }
                    for word in segment.words
                ],
            }
            for segment in document.segments
        ],
    }


def _payload_to_document(payload: dict[str, Any]) -> TranscriptDocument:
    segments = tuple(
        TranscriptSegment(
            start=segment["start"],
            end=segment["end"],
            text=segment["text"],
            words=tuple(
                TranscriptWord(
                    text=word["text"],
                    start=word["start"],
                    end=word["end"],
                    probability=word["probability"],
                )
                for word in segment["words"]
            ),
        )
        for segment in payload["segments"]
    )
    return TranscriptDocument(
        language=payload["language"], duration=payload["duration"], segments=segments
    )


class TranscriptRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_for_source(self, source_id: UUID, document: TranscriptDocument) -> TranscriptRecord:
        record = self.get_for_source(source_id)
        if record is None:
            record = TranscriptRecord(source_id=source_id)
            self.session.add(record)
        record.language = document.language
        record.duration = document.duration
        record.payload = _document_to_payload(document)
        self.session.flush()
        return record

    def get_for_source(self, source_id: UUID) -> TranscriptRecord | None:
        return (
            self.session.query(TranscriptRecord)
            .filter(TranscriptRecord.source_id == source_id)
            .one_or_none()
        )

    def get_document(self, source_id: UUID) -> TranscriptDocument | None:
        record = self.get_for_source(source_id)
        if record is None:
            return None
        return _payload_to_document(record.payload)


class PublicationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        clip_id: UUID,
        platform: Platform,
        scheduled_at: datetime,
    ) -> PublicationRecord:
        record = PublicationRecord(
            clip_id=clip_id, platform=platform, scheduled_at=scheduled_at
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get(self, publication_id: UUID) -> PublicationRecord | None:
        return self.session.get(PublicationRecord, publication_id)

    def list_all(
        self,
        *,
        platform: Platform | None = None,
        status: PublicationStatus | None = None,
        limit: int = 100,
    ) -> list[PublicationRecord]:
        query = self.session.query(PublicationRecord)
        if platform is not None:
            query = query.filter(PublicationRecord.platform == platform)
        if status is not None:
            query = query.filter(PublicationRecord.status == status)
        return (
            query.order_by(PublicationRecord.scheduled_at.asc(), PublicationRecord.id)
            .limit(limit)
            .all()
        )

    def due(self, now: datetime, *, limit: int = 50) -> list[PublicationRecord]:
        return (
            self.session.query(PublicationRecord)
            .filter(
                PublicationRecord.status == PublicationStatus.SCHEDULED,
                PublicationRecord.scheduled_at <= now,
            )
            .order_by(PublicationRecord.scheduled_at.asc())
            .limit(limit)
            .all()
        )

    def transition(
        self,
        publication_id: UUID,
        event: PublicationEvent,
        *,
        error: str | None = None,
    ) -> PublicationRecord:
        record = self.session.get(PublicationRecord, publication_id)
        if record is None:
            raise KeyError(publication_id)
        record.status = PublicationStateMachine.transition(record.status, event)
        if event is PublicationEvent.START:
            record.attempts += 1
        if error is not None:
            record.error = error
        self.session.flush()
        return record

    def attach_result(
        self,
        publication_id: UUID,
        *,
        external_id: str | None,
        external_url: str | None,
    ) -> PublicationRecord:
        record = self.session.get(PublicationRecord, publication_id)
        if record is None:
            raise KeyError(publication_id)
        record.external_id = external_id
        record.external_url = external_url
        record.published_at = datetime.now(UTC)
        record.status = PublicationStateMachine.transition(
            record.status, PublicationEvent.SUCCEED
        )
        self.session.flush()
        return record

    def reschedule_after_failure(
        self, publication_id: UUID, next_attempt_at: datetime
    ) -> PublicationRecord:
        """Apply bounded retry semantics: RETRY then push the schedule out."""
        record = self.transition(publication_id, PublicationEvent.RETRY)
        record.scheduled_at = next_attempt_at
        self.session.flush()
        return record

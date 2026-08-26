from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from openclips.domain.clips import ClipEvent, ClipStateMachine
from openclips.domain.jobs import JobEvent, JobStateMachine
from openclips.domain.sources import SourceEvent, SourceKind, SourceStateMachine
from openclips.domain.transcripts import TranscriptDocument, TranscriptSegment, TranscriptWord
from openclips.infrastructure.models import (
    ClipRecord,
    JobRecord,
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

    def get(self, job_id: UUID) -> JobRecord | None:
        return self.session.get(JobRecord, job_id)

    def transition(
        self, job_id: UUID, event: JobEvent, *, error: str | None = None
    ) -> JobRecord:
        record = self.session.get(JobRecord, job_id)
        if record is None:
            raise KeyError(job_id)
        record.status = JobStateMachine.transition(record.status, event)
        if event in (JobEvent.START, JobEvent.RETRY):
            record.attempts += 1
        if error is not None:
            record.error = error
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

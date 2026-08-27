"""Review API routes: catalog reads, lifecycle mutations, and job dispatch."""

from collections.abc import Callable, Iterator
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from openclips.api.schemas import (
    BulkActionBody,
    BulkResultItem,
    CaptionEditsBody,
    ClipEditBody,
    ClipOut,
    EnqueueJobOut,
    JobOut,
    SourceIngestOut,
    SourceOut,
    TranscriptionReadinessOut,
    YouTubeIngestBody,
)
from openclips.application.clipping import ClipSelectionCoordinator
from openclips.application.ingestion import IngestionCoordinator
from openclips.application.rendering import RenderCoordinator
from openclips.application.services import AppServices
from openclips.application.transcription import TranscriptionCoordinator
from openclips.application.youtube_ingestion import YouTubeIngestionCoordinator
from openclips.domain.clips import ClipEvent, ClipStatus
from openclips.domain.errors import InvalidTransitionError
from openclips.domain.jobs import JobStatus
from openclips.infrastructure.repositories import (
    ClipRepository,
    JobRepository,
    SourceRepository,
    TranscriptRepository,
)
from openclips.providers.local_upload import LocalUploadIngestor, UnsupportedUploadError
from openclips.providers.youtube import UnsupportedMediaLocator

SessionFactory = Callable[[], Session]

_UPLOAD_CHUNK_SIZE = 65536


class UploadTooLargeError(ValueError):
    """Raised when a streamed upload exceeds the configured byte budget."""


def _too_large(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=detail
    )


def _bounded_upload_chunks(upload: UploadFile, max_bytes: int) -> Iterator[bytes]:
    """Yield the upload in fixed chunks, refusing to cross the byte budget."""
    total = 0
    while chunk := upload.file.read(_UPLOAD_CHUNK_SIZE):
        total += len(chunk)
        if total > max_bytes:
            msg = f"Upload exceeds the maximum of {max_bytes} bytes"
            raise UploadTooLargeError(msg)
        yield chunk


def _not_found(entity: str, entity_id: UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=f"{entity} {entity_id} not found"
    )


def _conflict(detail: str | Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(detail))


def build_router(
    *,
    get_session: Callable[..., object],
    require_admin: Callable[..., None],
    services: AppServices,
) -> APIRouter:
    """Assemble all review endpoints around request-scoped repositories."""

    router = APIRouter(prefix="/api/v1")

    def _repos(session: Session) -> tuple[SourceRepository, JobRepository, ClipRepository]:
        return (
            SourceRepository(session),
            JobRepository(session),
            ClipRepository(session),
        )

    @router.get("/sources", response_model=list[SourceOut])
    def list_sources(session: Session = Depends(get_session)) -> list[SourceOut]:
        sources, _, _ = _repos(session)
        return [SourceOut.model_validate(record) for record in sources.list_all()]

    @router.get("/sources/{source_id}", response_model=SourceOut)
    def get_source(source_id: UUID, session: Session = Depends(get_session)) -> SourceOut:
        sources, _, _ = _repos(session)
        record = sources.get(source_id)
        if record is None:
            raise _not_found("Source", source_id)
        return SourceOut.model_validate(record)

    def _enqueue_transcription(
        session: Session, source_id: UUID
    ) -> EnqueueJobOut:
        sources, jobs, _ = _repos(session)
        coordinator = TranscriptionCoordinator(
            sources=sources,
            transcripts=TranscriptRepository(session),
            jobs=jobs,
            provider=services.transcription_provider,
            storage=services.storage,
        )
        job = coordinator.enqueue(source_id)
        return EnqueueJobOut(job_id=job.id, kind=job.kind, status=job.status.value)

    @router.post(
        "/sources/upload",
        response_model=SourceIngestOut,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_admin)],
    )
    def upload_source(
        file: UploadFile = File(...),
        auto_process: bool = Form(default=True),
        session: Session = Depends(get_session),
    ) -> SourceIngestOut:
        ingestor = LocalUploadIngestor(
            IngestionCoordinator(SourceRepository(session), services.storage)
        )
        try:
            source = ingestor.ingest(
                file.filename or "upload.mp4",
                _bounded_upload_chunks(file, services.max_upload_bytes),
                auto_process=auto_process,
            )
        except UploadTooLargeError as error:
            raise _too_large(str(error)) from error
        except UnsupportedUploadError as error:
            raise _conflict(error) from error
        next_job = _enqueue_transcription(session, source.id) if source.auto_process else None
        return SourceIngestOut(source=SourceOut.model_validate(source), next_job=next_job)

    @router.post(
        "/sources/youtube",
        response_model=SourceIngestOut,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_admin)],
    )
    def register_youtube_source(
        body: YouTubeIngestBody, session: Session = Depends(get_session)
    ) -> SourceIngestOut:
        coordinator = YouTubeIngestionCoordinator(
            sources=SourceRepository(session),
            jobs=JobRepository(session),
            storage=services.storage,
            downloader=services.downloader,
        )
        try:
            source, job = coordinator.register(body.url, auto_process=body.auto_process)
        except UnsupportedMediaLocator as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
            ) from error
        next_job = EnqueueJobOut(job_id=job.id, kind=job.kind, status=job.status.value)
        return SourceIngestOut(source=SourceOut.model_validate(source), next_job=next_job)

    @router.get("/system/transcription-readiness", response_model=TranscriptionReadinessOut)
    def transcription_readiness() -> TranscriptionReadinessOut:
        state = services.transcription_provider.readiness_state()
        return TranscriptionReadinessOut(status=state)

    @router.post(
        "/sources/{source_id}/transcribe",
        response_model=EnqueueJobOut,
        dependencies=[Depends(require_admin)],
    )
    def enqueue_transcribe(
        source_id: UUID, session: Session = Depends(get_session)
    ) -> EnqueueJobOut:
        sources, jobs, _ = _repos(session)
        coordinator = TranscriptionCoordinator(
            sources=sources,
            transcripts=TranscriptRepository(session),
            jobs=jobs,
            provider=services.transcription_provider,
            storage=services.storage,
        )
        try:
            job = coordinator.enqueue(source_id)
        except KeyError as error:
            raise _not_found("Source", source_id) from error
        except ValueError as error:
            raise _conflict(error) from error
        return EnqueueJobOut(job_id=job.id, kind=job.kind, status=job.status.value)

    @router.post(
        "/sources/{source_id}/select-clips",
        response_model=EnqueueJobOut,
        dependencies=[Depends(require_admin)],
    )
    def enqueue_select_clips(
        source_id: UUID, session: Session = Depends(get_session)
    ) -> EnqueueJobOut:
        sources, jobs, clips = _repos(session)
        coordinator = ClipSelectionCoordinator(
            sources=sources,
            transcripts=TranscriptRepository(session),
            clips=clips,
            jobs=jobs,
            refiner=services.refiner,
            bounds=services.bounds,
        )
        try:
            job = coordinator.enqueue(source_id)
        except KeyError as error:
            raise _not_found("Source", source_id) from error
        except ValueError as error:
            raise _conflict(error) from error
        return EnqueueJobOut(job_id=job.id, kind=job.kind, status=job.status.value)

    @router.get("/jobs", response_model=list[JobOut])
    def list_jobs(
        job_status: str | None = None,
        kind: str | None = None,
        session: Session = Depends(get_session),
    ) -> list[JobOut]:
        _, jobs, _ = _repos(session)
        resolved_status = None
        if job_status is not None:
            try:
                resolved_status = JobStatus(job_status.upper())
            except ValueError as error:
                raise _conflict(f"Unknown job status {job_status!r}") from error
        records = jobs.list_all(status=resolved_status, kind=kind)
        return [JobOut.model_validate(record) for record in records]

    @router.get("/jobs/{job_id}", response_model=JobOut)
    def get_job(job_id: UUID, session: Session = Depends(get_session)) -> JobOut:
        _, jobs, _ = _repos(session)
        record = jobs.get(job_id)
        if record is None:
            raise _not_found("Job", job_id)
        return JobOut.model_validate(record)

    @router.post(
        "/jobs/{job_id}/retry",
        response_model=JobOut,
        dependencies=[Depends(require_admin)],
    )
    def retry_job(job_id: UUID, session: Session = Depends(get_session)) -> JobOut:
        _, jobs, _ = _repos(session)
        try:
            job, _event = jobs.retry_dispatched(job_id)
        except KeyError as error:
            raise _not_found("Job", job_id) from error
        except InvalidTransitionError as error:
            raise _conflict(error) from error
        return JobOut.model_validate(job)

    @router.get("/clips", response_model=list[ClipOut])
    def review_queue(
        review_status: str | None = None,
        limit: int = 100,
        session: Session = Depends(get_session),
    ) -> list[ClipOut]:
        _, _, clips = _repos(session)
        resolved_status = None
        if review_status is not None:
            try:
                resolved_status = ClipStatus(review_status.upper())
            except ValueError as error:
                raise _conflict(f"Unknown clip status {review_status!r}") from error
        records = clips.list_all(status=resolved_status, limit=limit)
        return [ClipOut.model_validate(record) for record in records]

    @router.get("/clips/{clip_id}", response_model=ClipOut)
    def get_clip(clip_id: UUID, session: Session = Depends(get_session)) -> ClipOut:
        _, _, clips = _repos(session)
        record = clips.get(clip_id)
        if record is None:
            raise _not_found("Clip", clip_id)
        return ClipOut.model_validate(record)

    def _apply_edit_event(clips: ClipRepository, clip_id: UUID) -> None:
        try:
            clips.transition(clip_id, ClipEvent.EDIT)
        except KeyError as error:
            raise _not_found("Clip", clip_id) from error
        except InvalidTransitionError as error:
            raise _conflict(error) from error

    @router.patch("/clips/{clip_id}", response_model=ClipOut)
    def edit_clip(
        clip_id: UUID,
        body: ClipEditBody,
        session: Session = Depends(get_session),
        _admin: None = Depends(require_admin),
    ) -> ClipOut:
        _, _, clips = _repos(session)
        if clips.get(clip_id) is None:
            raise _not_found("Clip", clip_id)
        if body.title is not None:
            clips.set_title(clip_id, body.title)
        if body.start_time is not None or body.end_time is not None:
            clips.set_timespan(
                clip_id, start_time=body.start_time, end_time=body.end_time
            )
        _apply_edit_event(clips, clip_id)
        updated = clips.get(clip_id)
        assert updated is not None
        return ClipOut.model_validate(updated)

    @router.put("/clips/{clip_id}/caption-edits", response_model=ClipOut)
    def set_caption_edits(
        clip_id: UUID,
        body: CaptionEditsBody,
        session: Session = Depends(get_session),
        _admin: None = Depends(require_admin),
    ) -> ClipOut:
        _, _, clips = _repos(session)
        if clips.get(clip_id) is None:
            raise _not_found("Clip", clip_id)
        clips.set_caption_edits(
            clip_id, [{"match": e.match, "replacement": e.replacement} for e in body.edits]
        )
        _apply_edit_event(clips, clip_id)
        updated = clips.get(clip_id)
        assert updated is not None
        return ClipOut.model_validate(updated)

    def _review_transition(
        clip_id: UUID,
        event: ClipEvent,
        session: Session,
    ) -> ClipOut:
        _, _, clips = _repos(session)
        record = clips.get(clip_id)
        if record is None:
            raise _not_found("Clip", clip_id)
        try:
            updated = clips.transition(clip_id, event)
        except InvalidTransitionError as error:
            raise _conflict(error) from error
        except SQLAlchemyError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error
        return ClipOut.model_validate(updated)

    @router.post(
        "/clips/{clip_id}/approve",
        response_model=ClipOut,
        dependencies=[Depends(require_admin)],
    )
    def approve_clip(clip_id: UUID, session: Session = Depends(get_session)) -> ClipOut:
        return _review_transition(clip_id, ClipEvent.APPROVE, session)

    @router.post(
        "/clips/{clip_id}/reject",
        response_model=ClipOut,
        dependencies=[Depends(require_admin)],
    )
    def reject_clip(clip_id: UUID, session: Session = Depends(get_session)) -> ClipOut:
        return _review_transition(clip_id, ClipEvent.REJECT, session)

    @router.post("/clips/bulk", response_model=list[BulkResultItem])
    def bulk_action(
        body: BulkActionBody,
        session: Session = Depends(get_session),
        _admin: None = Depends(require_admin),
    ) -> list[BulkResultItem]:
        event = ClipEvent.APPROVE if body.action == "approve" else ClipEvent.REJECT
        results: list[BulkResultItem] = []
        _, _, clips = _repos(session)
        for clip_id in body.clip_ids:
            record = clips.get(clip_id)
            if record is None:
                results.append(BulkResultItem(clip_id=clip_id, ok=False, error="not found"))
                continue
            try:
                updated = clips.transition(clip_id, event)
                results.append(
                    BulkResultItem(clip_id=clip_id, ok=True, status=updated.status.value)
                )
            except InvalidTransitionError as error:
                results.append(BulkResultItem(clip_id=clip_id, ok=False, error=str(error)))
        return results

    @router.post(
        "/clips/{clip_id}/render",
        response_model=EnqueueJobOut,
        dependencies=[Depends(require_admin)],
    )
    def enqueue_render(clip_id: UUID, session: Session = Depends(get_session)) -> EnqueueJobOut:
        _, jobs, clips = _repos(session)
        coordinator = RenderCoordinator(
            clips=clips,
            sources=SourceRepository(session),
            transcripts=TranscriptRepository(session),
            jobs=jobs,
            renderer=services.renderer,
            storage=services.storage,
            style=services.style,
            crop_strategy=services.crop_strategy,
            width=services.width,
            height=services.height,
        )
        try:
            job = coordinator.enqueue(clip_id)
        except KeyError as error:
            raise _not_found("Clip", clip_id) from error
        except ValueError as error:
            raise _conflict(error) from error
        return EnqueueJobOut(job_id=job.id, kind=job.kind, status=job.status.value)

    @router.get("/dashboard", response_class=HTMLResponse)
    def dashboard(session: Session = Depends(get_session)) -> HTMLResponse:
        _, _, clips = _repos(session)
        records = clips.list_all(limit=200)
        rows = "".join(
            f"<tr><td>{record.title or 'Untitled'}</td>"
            f"<td>{record.status.value}</td>"
            f"<td>{record.selection_score if record.selection_score is not None else ''}</td></tr>"
            for record in records
        )
        html = (
            "<html><head><title>OpenClips review</title></head><body>"
            "<h1>Review queue</h1>"
            "<table border='1'><tr><th>Title</th><th>Status</th><th>Score</th></tr>"
            f"{rows}</table>"
            "</body></html>"
        )
        return HTMLResponse(content=html)

    return router

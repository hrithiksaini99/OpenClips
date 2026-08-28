"""Working V1 publication endpoints: public reads and admin scheduling.

These routes make the existing approved-clip scheduler and the atomic
publication dispatcher operable over HTTP. They reuse the review router's
``get_session``, ``require_admin`` and ``AppServices`` dependencies and add no
new auth or session mechanism. Copy persistence and automatic daily-window
configuration are deliberately out of scope for this task.
"""

from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from openclips.api.schemas import (
    BulkPublicationResultItem,
    BulkSchedulePublicationBody,
    PublicationOut,
    SchedulePublicationBody,
)
from openclips.application.publishing import (
    ClipNotApprovedError,
    ScheduleCoordinator,
    SchedulingExhaustedError,
)
from openclips.application.services import AppServices
from openclips.domain.errors import InvalidTransitionError
from openclips.domain.publishing import Platform, PublicationEvent, PublicationStatus
from openclips.infrastructure.models import PublicationRecord
from openclips.infrastructure.repositories import (
    ClipRepository,
    JobRepository,
    PublicationRepository,
)
from openclips.providers.media_urls import UnavailableMediaUrlProvider

_PUBLIC_MEDIA_SETTING = "OPENCLIPS_PUBLIC_MEDIA_BASE_URL"


def _not_found(publication_id: UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Publication {publication_id} not found",
    )


def _conflict(detail: str | Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(detail))


def add_publishing_routes(
    router: APIRouter,
    *,
    get_session: Callable[..., object],
    require_admin: Callable[..., None],
    services: AppServices,
) -> None:
    """Attach the publication endpoints to the shared ``/api/v1`` router."""

    def _coordinator(session: Session) -> ScheduleCoordinator:
        return ScheduleCoordinator(
            clips=ClipRepository(session),
            publications=PublicationRepository(session),
            jobs=JobRepository(session),
        )

    def _guard_platform_available(platform: Platform) -> None:
        """Reject Instagram scheduling before any row is created when the
        configured public media URL provider is unavailable."""
        if platform is Platform.INSTAGRAM_REELS and isinstance(
            services.media_url_provider, UnavailableMediaUrlProvider
        ):
            raise _conflict(
                "Instagram scheduling is unavailable until "
                f"{_PUBLIC_MEDIA_SETTING} is configured with a publicly reachable "
                "base URL"
            )

    def _schedule_one(
        coordinator: ScheduleCoordinator, body: SchedulePublicationBody, clip_id: UUID
    ) -> PublicationRecord:
        return coordinator.schedule(
            clip_id, body.platform, scheduled_at=body.scheduled_at
        )

    @router.get("/publications", response_model=list[PublicationOut])
    def list_publications(
        platform: Platform | None = None,
        publication_status: PublicationStatus | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        session: Session = Depends(get_session),
    ) -> list[PublicationOut]:
        records = PublicationRepository(session).list_all(
            platform=platform, status=publication_status, limit=limit
        )
        return [PublicationOut.model_validate(record) for record in records]

    @router.get("/publications/{publication_id}", response_model=PublicationOut)
    def get_publication(
        publication_id: UUID, session: Session = Depends(get_session)
    ) -> PublicationOut:
        record = PublicationRepository(session).get(publication_id)
        if record is None:
            raise _not_found(publication_id)
        return PublicationOut.model_validate(record)

    @router.post(
        "/publications",
        response_model=PublicationOut,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_admin)],
    )
    def schedule_publication(
        body: SchedulePublicationBody, session: Session = Depends(get_session)
    ) -> PublicationOut:
        _guard_platform_available(body.platform)
        try:
            record = _schedule_one(_coordinator(session), body, body.clip_id)
        except KeyError as error:
            raise _not_found(body.clip_id) from error
        except ClipNotApprovedError as error:
            raise _conflict(error) from error
        return PublicationOut.model_validate(record)

    @router.post(
        "/publications/bulk",
        response_model=list[BulkPublicationResultItem],
        dependencies=[Depends(require_admin)],
    )
    def bulk_schedule_publications(
        body: BulkSchedulePublicationBody, session: Session = Depends(get_session)
    ) -> list[BulkPublicationResultItem]:
        _guard_platform_available(body.platform)
        coordinator = _coordinator(session)
        results: list[BulkPublicationResultItem] = []
        for clip_id in body.clip_ids:
            single = SchedulePublicationBody(
                clip_id=clip_id,
                platform=body.platform,
                scheduled_at=body.scheduled_at,
            )
            try:
                record = _schedule_one(coordinator, single, clip_id)
            except KeyError:
                results.append(
                    BulkPublicationResultItem(
                        clip_id=clip_id, ok=False, error="clip not found"
                    )
                )
            except ClipNotApprovedError as error:
                results.append(
                    BulkPublicationResultItem(
                        clip_id=clip_id, ok=False, error=str(error)
                    )
                )
            else:
                results.append(
                    BulkPublicationResultItem(
                        clip_id=clip_id, ok=True, publication_id=record.id
                    )
                )
        return results

    @router.post(
        "/publications/{publication_id}/retry",
        response_model=PublicationOut,
        dependencies=[Depends(require_admin)],
    )
    def retry_publication(
        publication_id: UUID, session: Session = Depends(get_session)
    ) -> PublicationOut:
        try:
            record = _coordinator(session).retry(publication_id)
        except KeyError as error:
            raise _not_found(publication_id) from error
        except (SchedulingExhaustedError, InvalidTransitionError) as error:
            raise _conflict(error) from error
        return PublicationOut.model_validate(record)

    @router.post(
        "/publications/{publication_id}/cancel",
        response_model=PublicationOut,
        dependencies=[Depends(require_admin)],
    )
    def cancel_publication(
        publication_id: UUID, session: Session = Depends(get_session)
    ) -> PublicationOut:
        publications = PublicationRepository(session)
        try:
            record = publications.transition(publication_id, PublicationEvent.CANCEL)
        except KeyError as error:
            raise _not_found(publication_id) from error
        except InvalidTransitionError as error:
            raise _conflict(error) from error
        return PublicationOut.model_validate(record)

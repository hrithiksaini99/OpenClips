"""Publishing orchestration: scheduling, dispatch, and bounded retries."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import UUID

from openclips.domain.clips import ClipEvent, ClipStatus
from openclips.domain.jobs import JobEvent, JobStatus  # noqa: F401
from openclips.domain.publishing import (
    MAX_PUBLICATION_ATTEMPTS,
    Platform,
    PublicationEvent,
    PublicationStatus,
    can_retry,
    next_retry_at,
)
from openclips.infrastructure.media_storage import MediaStorage
from openclips.infrastructure.models import JobRecord, PublicationRecord
from openclips.infrastructure.repositories import (
    ClipRepository,
    JobRepository,
    PublicationRepository,
)
from openclips.providers.media_urls import PublicMediaUrlProvider
from openclips.providers.platforms.base import (
    PlatformPublisher,
    PublishError,
    PublishRequest,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DailyWindowRule:
    """Rule-based schedule: publish at fixed UTC times each day."""

    times: tuple[time, ...]

    def __post_init__(self) -> None:
        if not self.times:
            msg = "Daily window rule requires at least one time"
            raise ValueError(msg)

    def next_slot(self, now: datetime) -> datetime:
        """Return the next rule slot strictly after ``now``."""
        if now.tzinfo is None:
            msg = "Rule scheduling requires a timezone-aware timestamp"
            raise ValueError(msg)
        candidates: list[datetime] = []
        for slot_time in self.times:
            same_day = datetime.combine(now.date(), slot_time, tzinfo=UTC)
            if same_day > now:
                candidates.append(same_day)
            else:
                candidates.append(same_day + timedelta(days=1))
        return min(candidates)


class ClipNotApprovedError(ValueError):
    """Raised when a clip is not APPROVED and therefore cannot be scheduled."""


class SchedulingExhaustedError(ValueError):
    """Raised when a publication has no attempts left in its bounded budget."""


def _failure_message(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


class ScheduleCoordinator:
    """Creates publication records for approved clips and runs publish jobs.

    Each platform owns an independent queue and job kind. Failures preserve
    state and reason on the publication record and reschedule deterministically
    within the bounded attempt budget.

    ``publishers`` and ``storage`` are only needed to run an attempt, so a
    dispatch-only coordinator (see ``PublicationScheduler``) may omit them.
    """

    def __init__(
        self,
        *,
        clips: ClipRepository,
        publications: PublicationRepository,
        jobs: JobRepository,
        publishers: dict[Platform, PlatformPublisher] | None = None,
        storage: MediaStorage | None = None,
        clock: Callable[[], datetime] | None = None,
        media_url_provider: PublicMediaUrlProvider | None = None,
    ) -> None:
        self.clips = clips
        self.publications = publications
        self.jobs = jobs
        self.publishers = publishers if publishers is not None else {}
        self.storage = storage
        self._clock = clock or (lambda: datetime.now(UTC))
        self._media_url_provider = media_url_provider

    def schedule(
        self,
        clip_id: UUID,
        platform: Platform,
        *,
        scheduled_at: datetime | None = None,
    ) -> PublicationRecord:
        """Schedule one approved clip on one platform."""
        clip = self.clips.get(clip_id)
        if clip is None:
            raise KeyError(clip_id)
        if clip.status is not ClipStatus.APPROVED:
            msg = f"Only approved clips can be scheduled; clip {clip_id} is {clip.status}"
            raise ClipNotApprovedError(msg)

        moment = scheduled_at or self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        record = self.publications.create(
            clip_id=clip_id, platform=platform, scheduled_at=moment
        )
        self.clips.transition(clip_id, ClipEvent.SCHEDULE)
        return record

    def schedule_by_rule(
        self,
        clip_ids: list[UUID],
        platform: Platform,
        rule: "DailyWindowRule",
    ) -> list[PublicationRecord]:
        """Assign consecutive rule slots to each clip deterministically."""
        records: list[PublicationRecord] = []
        base_slot = rule.next_slot(self._clock())
        for offset, clip_id in enumerate(clip_ids):
            slot = base_slot + timedelta(minutes=offset)
            records.append(self.schedule(clip_id, platform, scheduled_at=slot))
        return records

    def run(self, job: JobRecord) -> PublicationRecord:
        """Execute one claimed publish job body without touching state."""
        if not job.payload:
            msg = f"Publish job {job.id} has no publication payload"
            raise PublishError(msg)
        return self.publish_publication(UUID(job.payload))

    def publish_publication(self, publication_id: UUID) -> PublicationRecord:
        """Attempt one QUEUED publication; failures reschedule with bounded backoff.

        A publication cancelled after its job was queued is returned untouched so
        the redelivered message can be acknowledged without reaching the platform.
        Any other non-``QUEUED`` status raises ``InvalidTransitionError``.
        """
        record = self._get(publication_id)
        if record.status is PublicationStatus.CANCELLED:
            logger.info("Skipping cancelled publication %s", record.id)
            return record
        self.publications.transition(record.id, PublicationEvent.START)
        publisher = self.publishers.get(record.platform)
        if publisher is None:
            return self._fail(record, "NoPublisherRegistered")
        try:
            result = publisher.publish(self._request_for(record))
        except PublishError as error:
            return self._fail(record, _failure_message(error))

        published = self.publications.attach_result(
            record.id,
            external_id=result.external_id,
            external_url=result.external_url,
        )
        self.clips.transition(record.clip_id, ClipEvent.PUBLISH)
        logger.info(
            "Published %s to %s as %s",
            record.clip_id,
            record.platform,
            result.external_id,
        )
        return published

    def retry(self, publication_id: UUID) -> PublicationRecord:
        """Requeue a failed publication within its bounded attempt budget."""
        record = self._get(publication_id)
        if not can_retry(record.attempts):
            msg = (
                f"Publication {publication_id} exhausted its "
                f"{MAX_PUBLICATION_ATTEMPTS} attempt budget"
            )
            raise SchedulingExhaustedError(msg)
        return self.publications.reschedule_after_failure(
            record.id, next_retry_at(self._clock(), record.attempts)
        )

    def enqueue_due(self, *, limit: int = 50) -> list[JobRecord]:
        """Queue every due publication once, on its own platform queue.

        Claiming, the ``ENQUEUE`` transition and the outbox row all belong to the
        caller's single transaction: a poll that commits has moved each record out
        of ``SCHEDULED``, so a later poll cannot enqueue it again, and a poll that
        rolls back leaves no job behind.
        """
        jobs: list[JobRecord] = []
        for record in self.publications.claim_due(self._clock(), limit):
            self.publications.transition(record.id, PublicationEvent.ENQUEUE)
            job, _event = self.jobs.create_dispatched(
                record.platform.job_kind,
                payload=str(record.id),
                queue_name=record.platform.job_kind,
            )
            jobs.append(job)
        return jobs

    def _get(self, publication_id: UUID) -> PublicationRecord:
        record = self.publications.get(publication_id)
        if record is None:
            raise KeyError(publication_id)
        return record

    def _request_for(self, record: PublicationRecord) -> PublishRequest:
        if self.storage is None:
            msg = "This coordinator was built for dispatch only and cannot read clip media"
            raise PublishError(msg)
        clip = self.clips.get(record.clip_id)
        if clip is None or not clip.output_path:
            msg = f"Clip {record.clip_id} has no rendered media to publish"
            raise PublishError(msg)
        media = self.storage.resolve(str(clip.output_path))
        title = clip.title or "OpenClips"
        media_url: str | None = None
        if record.platform is Platform.INSTAGRAM_REELS:
            if self._media_url_provider is None:
                msg = (
                    "Instagram publishing requires a public media URL provider; "
                    "set OPENCLIPS_PUBLIC_MEDIA_BASE_URL"
                )
                raise PublishError(msg)
            media_url = self._media_url_provider.resolve(record.clip_id)
        return PublishRequest(clip_media=media, title=title, media_url=media_url)

    def _fail(self, record: PublicationRecord, message: str) -> PublicationRecord:
        failed = self.publications.transition(
            record.id, PublicationEvent.FAIL, error=message
        )
        if can_retry(failed.attempts):
            return self.publications.reschedule_after_failure(
                failed.id, next_retry_at(self._clock(), failed.attempts)
            )
        logger.warning("Publication %s exhausted retries: %s", record.id, message)
        return failed

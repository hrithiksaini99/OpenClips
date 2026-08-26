"""Publishing lifecycle, platform identities, and bounded retry policy."""

from datetime import UTC, datetime, timedelta
from enum import StrEnum

from openclips.domain.errors import InvalidTransitionError

MAX_PUBLICATION_ATTEMPTS = 5
_BACKOFF_BASE_SECONDS = 30
_BACKOFF_CAP_SECONDS = 3600


class Platform(StrEnum):
    INSTAGRAM_REELS = "INSTAGRAM_REELS"
    YOUTUBE_SHORTS = "YOUTUBE_SHORTS"

    @property
    def queue_name(self) -> str:
        return f"publish.{self.value.lower()}"

    @property
    def job_kind(self) -> str:
        return self.queue_name


class PublicationStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class PublicationEvent(StrEnum):
    START = "START"
    SUCCEED = "SUCCEED"
    FAIL = "FAIL"
    RETRY = "RETRY"


class PublicationStateMachine:
    _transitions = {
        (PublicationStatus.SCHEDULED, PublicationEvent.START): PublicationStatus.PUBLISHING,
        (PublicationStatus.PUBLISHING, PublicationEvent.SUCCEED): PublicationStatus.PUBLISHED,
        (PublicationStatus.PUBLISHING, PublicationEvent.FAIL): PublicationStatus.FAILED,
        (PublicationStatus.FAILED, PublicationEvent.RETRY): PublicationStatus.SCHEDULED,
    }

    @classmethod
    def transition(
        cls, current: PublicationStatus, event: PublicationEvent
    ) -> PublicationStatus:
        try:
            return cls._transitions[(current, event)]
        except KeyError as error:
            raise InvalidTransitionError(
                f"Cannot apply {event} to publication in {current}"
            ) from error


def backoff_seconds(attempts: int) -> int:
    """Deterministic exponential backoff capped at one hour."""
    if attempts < 1:
        msg = f"Backoff requires at least one attempt, got {attempts}"
        raise ValueError(msg)
    scaled: int = _BACKOFF_BASE_SECONDS * (2 ** (attempts - 1))
    return min(scaled, _BACKOFF_CAP_SECONDS)


def next_retry_at(now: datetime, attempts: int) -> datetime:
    """Return the earliest retry moment after ``attempts`` failed tries."""
    if now.tzinfo is None:
        msg = "Retry scheduling requires a timezone-aware timestamp"
        raise ValueError(msg)
    return now + timedelta(seconds=backoff_seconds(attempts))


def can_retry(attempts: int) -> bool:
    """Whether another attempt remains within the bounded budget."""
    return attempts < MAX_PUBLICATION_ATTEMPTS


def utc_now() -> datetime:
    return datetime.now(UTC)

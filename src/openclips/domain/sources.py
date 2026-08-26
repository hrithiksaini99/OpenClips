from enum import StrEnum

from openclips.domain.errors import InvalidTransitionError

SOURCE_RETENTION_DAYS = 7


class SourceKind(StrEnum):
    LOCAL_UPLOAD = "LOCAL_UPLOAD"
    YOUTUBE_VIDEO = "YOUTUBE_VIDEO"


class SourceStatus(StrEnum):
    PENDING = "PENDING"
    INGESTING = "INGESTING"
    READY = "READY"
    FAILED = "FAILED"


class SourceEvent(StrEnum):
    START = "START"
    SUCCEED = "SUCCEED"
    FAIL = "FAIL"
    RETRY = "RETRY"


class SourceStateMachine:
    _transitions = {
        (SourceStatus.PENDING, SourceEvent.START): SourceStatus.INGESTING,
        (SourceStatus.INGESTING, SourceEvent.SUCCEED): SourceStatus.READY,
        (SourceStatus.INGESTING, SourceEvent.FAIL): SourceStatus.FAILED,
        (SourceStatus.FAILED, SourceEvent.RETRY): SourceStatus.PENDING,
    }

    @classmethod
    def transition(cls, current: SourceStatus, event: SourceEvent) -> SourceStatus:
        try:
            return cls._transitions[(current, event)]
        except KeyError as error:
            raise InvalidTransitionError(f"Cannot apply {event} to source in {current}") from error

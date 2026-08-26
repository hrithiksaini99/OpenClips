from enum import StrEnum

from openclips.domain.errors import InvalidTransitionError


class ClipStatus(StrEnum):
    GENERATING = "GENERATING"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SCHEDULED = "SCHEDULED"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class ClipEvent(StrEnum):
    READY = "READY"
    EDIT = "EDIT"
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    SCHEDULE = "SCHEDULE"
    PUBLISH = "PUBLISH"
    FAIL = "FAIL"
    RETRY = "RETRY"


class ClipStateMachine:
    _transitions = {
        (ClipStatus.GENERATING, ClipEvent.READY): ClipStatus.READY_FOR_REVIEW,
        (ClipStatus.READY_FOR_REVIEW, ClipEvent.EDIT): ClipStatus.NEEDS_REVIEW,
        (ClipStatus.READY_FOR_REVIEW, ClipEvent.APPROVE): ClipStatus.APPROVED,
        (ClipStatus.READY_FOR_REVIEW, ClipEvent.REJECT): ClipStatus.REJECTED,
        (ClipStatus.NEEDS_REVIEW, ClipEvent.APPROVE): ClipStatus.APPROVED,
        (ClipStatus.NEEDS_REVIEW, ClipEvent.REJECT): ClipStatus.REJECTED,
        (ClipStatus.APPROVED, ClipEvent.EDIT): ClipStatus.NEEDS_REVIEW,
        (ClipStatus.APPROVED, ClipEvent.SCHEDULE): ClipStatus.SCHEDULED,
        (ClipStatus.SCHEDULED, ClipEvent.EDIT): ClipStatus.NEEDS_REVIEW,
        (ClipStatus.SCHEDULED, ClipEvent.PUBLISH): ClipStatus.PUBLISHED,
        (ClipStatus.SCHEDULED, ClipEvent.FAIL): ClipStatus.FAILED,
        (ClipStatus.FAILED, ClipEvent.RETRY): ClipStatus.SCHEDULED,
    }

    @classmethod
    def transition(cls, current: ClipStatus, event: ClipEvent) -> ClipStatus:
        try:
            return cls._transitions[(current, event)]
        except KeyError as error:
            raise InvalidTransitionError(f"Cannot apply {event} to clip in {current}") from error

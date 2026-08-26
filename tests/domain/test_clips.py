import pytest

from openclips.domain.clips import ClipEvent, ClipStateMachine, ClipStatus
from openclips.domain.errors import InvalidTransitionError


def test_clip_requires_approval_before_scheduling():
    status = ClipStateMachine.transition(ClipStatus.READY_FOR_REVIEW, ClipEvent.APPROVE)
    assert ClipStateMachine.transition(status, ClipEvent.SCHEDULE) == ClipStatus.SCHEDULED


def test_editing_scheduled_clip_returns_to_review():
    assert (
        ClipStateMachine.transition(ClipStatus.SCHEDULED, ClipEvent.EDIT)
        == ClipStatus.NEEDS_REVIEW
    )


def test_clip_cannot_schedule_before_approval():
    with pytest.raises(InvalidTransitionError):
        ClipStateMachine.transition(ClipStatus.READY_FOR_REVIEW, ClipEvent.SCHEDULE)

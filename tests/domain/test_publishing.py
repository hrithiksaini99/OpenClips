"""Publication lifecycle contract: every edge of the state machine."""

import pytest

from openclips.domain.errors import InvalidTransitionError
from openclips.domain.publishing import (
    PublicationEvent,
    PublicationStateMachine,
    PublicationStatus,
)

ALLOWED = [
    (PublicationStatus.SCHEDULED, PublicationEvent.ENQUEUE, PublicationStatus.QUEUED),
    (PublicationStatus.QUEUED, PublicationEvent.START, PublicationStatus.PUBLISHING),
    (PublicationStatus.PUBLISHING, PublicationEvent.SUCCEED, PublicationStatus.PUBLISHED),
    (PublicationStatus.PUBLISHING, PublicationEvent.FAIL, PublicationStatus.FAILED),
    (PublicationStatus.FAILED, PublicationEvent.RETRY, PublicationStatus.SCHEDULED),
    (PublicationStatus.SCHEDULED, PublicationEvent.CANCEL, PublicationStatus.CANCELLED),
    (PublicationStatus.QUEUED, PublicationEvent.CANCEL, PublicationStatus.CANCELLED),
    (PublicationStatus.FAILED, PublicationEvent.CANCEL, PublicationStatus.CANCELLED),
]


@pytest.mark.parametrize(("current", "event", "expected"), ALLOWED)
def test_allowed_transitions_reach_the_expected_status(
    current: PublicationStatus,
    event: PublicationEvent,
    expected: PublicationStatus,
) -> None:
    assert PublicationStateMachine.transition(current, event) is expected


def test_allowed_transitions_are_exactly_the_declared_edges() -> None:
    assert set(PublicationStateMachine._transitions) == {
        (current, event) for current, event, _expected in ALLOWED
    }


def test_scheduled_publications_must_be_queued_before_starting() -> None:
    with pytest.raises(InvalidTransitionError, match="START"):
        PublicationStateMachine.transition(
            PublicationStatus.SCHEDULED, PublicationEvent.START
        )


def test_queued_publication_can_be_cancelled() -> None:
    assert (
        PublicationStateMachine.transition(
            PublicationStatus.QUEUED, PublicationEvent.CANCEL
        )
        is PublicationStatus.CANCELLED
    )


def test_published_publication_cannot_be_cancelled() -> None:
    with pytest.raises(InvalidTransitionError, match="CANCEL"):
        PublicationStateMachine.transition(
            PublicationStatus.PUBLISHED, PublicationEvent.CANCEL
        )


def test_cancelled_publication_is_terminal() -> None:
    for event in PublicationEvent:
        with pytest.raises(InvalidTransitionError):
            PublicationStateMachine.transition(PublicationStatus.CANCELLED, event)

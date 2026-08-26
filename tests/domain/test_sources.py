from datetime import UTC, datetime, timedelta

import pytest

from openclips.domain.errors import InvalidTransitionError
from openclips.domain.sources import (
    SOURCE_RETENTION_DAYS,
    SourceEvent,
    SourceKind,
    SourceStateMachine,
    SourceStatus,
)


def test_source_kinds_cover_local_upload_and_youtube_video() -> None:
    assert set(SourceKind) == {SourceKind.LOCAL_UPLOAD, SourceKind.YOUTUBE_VIDEO}


def test_pending_moves_to_ingesting_then_ready() -> None:
    status = SourceStateMachine.transition(SourceStatus.PENDING, SourceEvent.START)
    assert status is SourceStatus.INGESTING
    assert SourceStateMachine.transition(status, SourceEvent.SUCCEED) is SourceStatus.READY


def test_ingesting_can_fail_and_failed_can_retry_to_pending() -> None:
    failed = SourceStateMachine.transition(SourceStatus.INGESTING, SourceEvent.FAIL)
    assert failed is SourceStatus.FAILED
    assert SourceStateMachine.transition(failed, SourceEvent.RETRY) is SourceStatus.PENDING


def test_failed_source_recovers_through_full_cycle() -> None:
    status = SourceStateMachine.transition(SourceStatus.PENDING, SourceEvent.START)
    status = SourceStateMachine.transition(status, SourceEvent.FAIL)
    status = SourceStateMachine.transition(status, SourceEvent.RETRY)
    assert status is SourceStatus.PENDING
    status = SourceStateMachine.transition(status, SourceEvent.START)
    assert SourceStateMachine.transition(status, SourceEvent.SUCCEED) is SourceStatus.READY


def test_ready_is_terminal() -> None:
    for event in SourceEvent:
        with pytest.raises(InvalidTransitionError, match="Cannot apply"):
            SourceStateMachine.transition(SourceStatus.READY, event)


def test_pending_rejects_events_other_than_start() -> None:
    for event in (SourceEvent.SUCCEED, SourceEvent.FAIL, SourceEvent.RETRY):
        with pytest.raises(InvalidTransitionError, match="Cannot apply"):
            SourceStateMachine.transition(SourceStatus.PENDING, event)


def test_ingesting_rejects_start_and_retry() -> None:
    for event in (SourceEvent.START, SourceEvent.RETRY):
        with pytest.raises(InvalidTransitionError, match="Cannot apply"):
            SourceStateMachine.transition(SourceStatus.INGESTING, event)


def test_failed_rejects_start_succeed_and_fail() -> None:
    for event in (SourceEvent.START, SourceEvent.SUCCEED, SourceEvent.FAIL):
        with pytest.raises(InvalidTransitionError, match="Cannot apply"):
            SourceStateMachine.transition(SourceStatus.FAILED, event)


def test_source_retention_is_exactly_seven_days() -> None:
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    assert SOURCE_RETENTION_DAYS == 7
    assert now + timedelta(days=SOURCE_RETENTION_DAYS) == datetime(
        2026, 9, 2, 12, 0, tzinfo=UTC
    )

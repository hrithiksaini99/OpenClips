"""When the scheduler decides to post.

This arithmetic runs unattended against a real channel, so the cases that matter
are the awkward ones: a slot that has only just passed, one the machine slept
through, and the roll over midnight.
"""

from __future__ import annotations

from datetime import datetime

from studio.publisher import (
    Board,
    Entry,
    Schedule,
    _daily_limit,
    due_slot,
    posted_today,
    upcoming,
)

SLOTS = ["09:00", "15:00", "20:00"]


def at(hour: int, minute: int = 0, day: int = 29) -> datetime:
    return datetime(2026, 8, day, hour, minute)


def test_no_slot_is_due_before_the_first_one() -> None:
    assert due_slot(Schedule(slots=SLOTS), [], at(8, 59)) is None


def test_slot_fires_on_the_hour() -> None:
    assert due_slot(Schedule(slots=SLOTS), [], at(9, 0)) == "2026-08-29T09:00"


def test_slot_still_fires_inside_the_catch_up_window() -> None:
    assert due_slot(Schedule(slots=SLOTS), [], at(10, 30)) == "2026-08-29T09:00"


def test_slot_is_abandoned_once_the_window_has_passed() -> None:
    # Otherwise starting the app in the evening would fire the whole day at once.
    assert due_slot(Schedule(slots=SLOTS), [], at(13, 0)) is None


def test_a_claimed_slot_does_not_fire_again() -> None:
    assert due_slot(Schedule(slots=SLOTS), ["2026-08-29T09:00"], at(9, 30)) is None


def test_the_next_slot_still_fires_after_an_earlier_claim() -> None:
    claimed = ["2026-08-29T09:00"]

    assert due_slot(Schedule(slots=SLOTS), claimed, at(15, 5)) == "2026-08-29T15:00"


def test_the_earliest_due_slot_wins_regardless_of_order() -> None:
    schedule = Schedule(slots=["20:00", "09:00"])

    assert due_slot(schedule, [], at(9, 30)) == "2026-08-29T09:00"


def test_a_late_evening_slot_fires_after_midnight() -> None:
    schedule = Schedule(slots=["23:30"])

    assert due_slot(schedule, [], at(0, 30, day=30)) == "2026-08-29T23:30"


def test_a_late_evening_slot_expires_like_any_other() -> None:
    schedule = Schedule(slots=["23:30"])

    assert due_slot(schedule, [], at(2, 30, day=30)) is None


def test_unparseable_times_are_skipped_not_fatal() -> None:
    schedule = Schedule(slots=["", "9", "ab:cd", "99:99", "15:00"])

    assert due_slot(schedule, [], at(15, 1)) == "2026-08-29T15:00"


def test_only_today_counts_towards_the_day() -> None:
    queue = [
        Entry("a", "j", "c", "f", "t", "d", status="posted", posted_at=at(9, 1).timestamp()),
        Entry("b", "j", "c", "f", "t", "d", status="posted",
              posted_at=at(20, 1, day=28).timestamp()),
        Entry("c", "j", "c", "f", "t", "d"),
    ]

    assert posted_today(queue, at(16, 0)) == 1


def test_the_number_of_times_chosen_is_the_number_posted() -> None:
    # There is no separate per-day control that could disagree with the slots.
    assert _daily_limit(Schedule(slots=["09:00"])) == 1
    assert _daily_limit(Schedule(slots=SLOTS)) == 3


def test_the_day_is_capped_at_youtubes_own_limit() -> None:
    every_hour = [f"{hour:02d}:00" for hour in range(24)]

    assert _daily_limit(Schedule(slots=every_hour)) == 24
    assert _daily_limit(Schedule(slots=[])) == 1


def test_projection_fills_today_then_rolls_over() -> None:
    board = Board(
        schedule=Schedule(enabled=True, slots=SLOTS),
        queue=[
            Entry("p1", "j", "c", "f", "t", "d", status="posted", posted_at=at(9, 1).timestamp()),
            Entry("p2", "j", "c", "f", "t", "d", status="posted", posted_at=at(15, 1).timestamp()),
            Entry("w1", "j", "c", "f", "t", "d"),
            Entry("w2", "j", "c", "f", "t", "d"),
        ],
    )

    assert upcoming(board, at(16, 0)) == {
        "w1": "2026-08-29T20:00",
        "w2": "2026-08-30T09:00",
    }


def test_a_disarmed_schedule_projects_nothing() -> None:
    board = Board(schedule=Schedule(enabled=False), queue=[Entry("w", "j", "c", "f", "t", "d")])

    assert upcoming(board, at(16, 0)) == {}

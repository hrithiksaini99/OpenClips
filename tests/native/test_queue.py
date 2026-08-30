"""Posting: claiming, failing, batching, and standing down.

The queue posts to a real channel without supervision, so the cases worth
holding onto are the ones where it must NOT act: an entry already in flight, a
batch behind a clip that hit the channel's cap, an upload interrupted by a
restart.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from studio import pipeline, publisher, youtube


def test_enqueue_writes_one_entry_per_clip(
    studio_home: Path, queued: Callable[..., list[publisher.Entry]]
) -> None:
    added = queued()

    assert [entry.clip_id for entry in added] == ["01-clip", "02-clip", "03-clip"]
    assert all(entry.status == "pending" for entry in added)


def test_enqueue_credits_the_source_episode(
    studio_home: Path, queued: Callable[..., list[publisher.Entry]]
) -> None:
    added = queued(clips=1)

    assert "youtu.be/abcdefghijk" in added[0].description


def test_a_clip_is_never_queued_twice(
    studio_home: Path, queued: Callable[..., list[publisher.Entry]]
) -> None:
    queued()

    assert publisher.enqueue("job") == []


def test_a_disarmed_schedule_posts_nothing(
    studio_home: Path, connected: None, queued: Callable[..., list[publisher.Entry]]
) -> None:
    queued()

    assert publisher.scheduler.tick(datetime(2026, 8, 29, 9, 5)) is None


def test_a_due_slot_posts_one_clip(
    studio_home: Path, connected: None, queued: Callable[..., list[publisher.Entry]],
    monkeypatch: pytest.MonkeyPatch
) -> None:
    queued()
    publisher.configure({"enabled": True, "slots": ["09:00", "15:00"]})
    sent: list[str] = []
    monkeypatch.setattr(publisher.youtube, "upload",
                        lambda path, **kw: sent.append(path.name) or "VID1")

    posted = publisher.scheduler.tick(datetime(2026, 8, 29, 9, 5))

    assert posted is not None
    assert sent == ["01-clip.mp4"]


def test_a_slot_cannot_post_twice(
    studio_home: Path, connected: None, queued: Callable[..., list[publisher.Entry]],
    monkeypatch: pytest.MonkeyPatch
) -> None:
    queued()
    publisher.configure({"enabled": True, "slots": ["09:00", "15:00"]})
    monkeypatch.setattr(publisher.youtube, "upload", lambda path, **kw: "VID")

    publisher.scheduler.tick(datetime(2026, 8, 29, 9, 5))

    assert publisher.scheduler.tick(datetime(2026, 8, 29, 9, 40)) is None


def test_a_failed_upload_retries_then_parks(
    studio_home: Path, connected: None, queued: Callable[..., list[publisher.Entry]],
    monkeypatch: pytest.MonkeyPatch
) -> None:
    queued(clips=1)
    publisher.configure({"enabled": True, "slots": ["09:00"]})
    monkeypatch.setattr(publisher.youtube, "upload",
                        lambda path, **kw: (_ for _ in ()).throw(RuntimeError("network died")))
    entry_id = publisher.load().queue[0].id

    for day in (29, 30, 31):
        publisher.scheduler.tick(datetime(2026, 8, day, 9, 1))

    entry = publisher.find(entry_id)
    assert entry is not None
    assert (entry.status, entry.attempts) == ("failed", publisher.MAX_ATTEMPTS)
    assert "network died" in entry.error


def test_a_parked_entry_can_be_revived(
    studio_home: Path, queued: Callable[..., list[publisher.Entry]]
) -> None:
    queued(clips=1)
    entry_id = publisher.load().queue[0].id
    publisher._finish(entry_id, error="boom")
    publisher._finish(entry_id, error="boom")
    publisher._finish(entry_id, error="boom")

    revived = publisher.retry(entry_id)

    assert revived is not None
    assert (revived.status, revived.attempts) == ("pending", 0)


def test_publish_now_refuses_without_an_account(
    studio_home: Path, queued: Callable[..., list[publisher.Entry]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued(clips=1)
    monkeypatch.setattr(publisher.youtube, "connected", lambda: False)
    entry_id = publisher.load().queue[0].id

    with pytest.raises(youtube.NotConnected):
        publisher.claim_now(entry_id)


def test_publish_now_refuses_an_entry_already_posted(
    studio_home: Path, connected: None, queued: Callable[..., list[publisher.Entry]]
) -> None:
    queued(clips=1)
    entry_id = publisher.load().queue[0].id
    publisher._finish(entry_id, video_id="VID")

    with pytest.raises(RuntimeError, match="already posted"):
        publisher.claim_now(entry_id)


def test_a_batch_claims_everything_waiting(
    studio_home: Path, connected: None, queued: Callable[..., list[publisher.Entry]]
) -> None:
    queued(clips=5)

    claimed = publisher.claim_batch()

    assert len(claimed) == 5
    assert {entry.status for entry in publisher.load().queue} == {"uploading"}


def test_a_claimed_batch_cannot_be_stolen_by_a_tick(
    studio_home: Path, connected: None, queued: Callable[..., list[publisher.Entry]]
) -> None:
    queued(clips=3)
    publisher.configure({"enabled": True, "slots": [f"{hour:02d}:00" for hour in range(24)]})
    publisher.claim_batch()

    assert publisher.scheduler._claim(datetime.now()) is None


def test_one_bad_clip_does_not_stop_the_batch(
    studio_home: Path, connected: None, queued: Callable[..., list[publisher.Entry]],
    monkeypatch: pytest.MonkeyPatch
) -> None:
    queued(clips=5)
    sent: list[str] = []

    def upload(path: Path, **_kw: object) -> str:
        sent.append(path.name)
        if len(sent) == 3:
            raise RuntimeError("network died")
        return f"VID{len(sent)}"

    monkeypatch.setattr(publisher.youtube, "upload", upload)
    publisher.scheduler.post_batch(publisher.claim_batch())

    queue = publisher.load().queue
    assert len(sent) == 5
    assert sum(1 for entry in queue if entry.status == "posted") == 4
    assert sum(1 for entry in queue if entry.status == "pending") == 1


def test_hitting_the_channel_cap_costs_no_attempt(
    studio_home: Path, connected: None, queued: Callable[..., list[publisher.Entry]],
    monkeypatch: pytest.MonkeyPatch
) -> None:
    # The cap is not the clip's fault and no number of retries clears it, so
    # spending attempts on it would park a queue that is merely early.
    queued(clips=4)
    sent: list[str] = []

    def upload(path: Path, **_kw: object) -> str:
        sent.append(path.name)
        if len(sent) >= 2:
            raise youtube.UploadLimitReached("daily upload limit")
        return "VID1"

    monkeypatch.setattr(publisher.youtube, "upload", upload)
    publisher.scheduler.post_batch(publisher.claim_batch())

    board = publisher.load()
    waiting = [entry for entry in board.queue if entry.status == "pending"]
    assert len(waiting) == 3
    assert {entry.attempts for entry in waiting} == {0}
    assert board.paused_until > time.time()


def test_a_paused_queue_declines_every_way_of_posting(
    studio_home: Path, connected: None, queued: Callable[..., list[publisher.Entry]]
) -> None:
    queued(clips=2)
    publisher.configure({"enabled": True, "slots": ["09:00"]})
    board = publisher.load()
    board.paused_until = time.time() + 3600
    publisher.save(board)

    assert publisher.scheduler._claim(datetime.now()) is None
    with pytest.raises(RuntimeError, match="daily upload limit"):
        publisher.claim_batch()
    with pytest.raises(RuntimeError, match="daily upload limit"):
        publisher.claim_now(publisher.load().queue[0].id)


def test_the_queue_resumes_once_the_pause_expires(
    studio_home: Path, connected: None, queued: Callable[..., list[publisher.Entry]]
) -> None:
    queued(clips=2)
    publisher.configure({"enabled": True, "slots": [f"{hour:02d}:00" for hour in range(24)]})
    board = publisher.load()
    board.paused_until = time.time() - 1
    publisher.save(board)

    assert publisher.scheduler._claim(datetime.now()) is not None


def test_an_interrupted_upload_that_landed_is_recorded_not_reposted(
    studio_home: Path, connected: None, queued: Callable[..., list[publisher.Entry]],
    monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reposting these is what put a duplicate on the channel: the clips had
    # reached YouTube, only the result was never written down.
    queued(clips=2)
    publisher.claim_batch()
    monkeypatch.setattr(publisher.youtube, "recent_uploads", lambda: {"Clip 1": "LANDED"})

    requeued = publisher.recover_stalled()

    queue = {entry.clip_id: entry for entry in publisher.load().queue}
    assert requeued == 1
    assert (queue["01-clip"].status, queue["01-clip"].video_id) == ("posted", "LANDED")
    assert queue["02-clip"].status == "pending"


def test_recovery_requeues_everything_when_youtube_cannot_be_reached(
    studio_home: Path, connected: None, queued: Callable[..., list[publisher.Entry]],
    monkeypatch: pytest.MonkeyPatch
) -> None:
    queued(clips=2)
    publisher.claim_batch()
    monkeypatch.setattr(publisher.youtube, "recent_uploads",
                        lambda: (_ for _ in ()).throw(RuntimeError("offline")))

    assert publisher.recover_stalled() == 2


def test_recovery_leaves_settled_entries_alone(
    studio_home: Path, queued: Callable[..., list[publisher.Entry]]
) -> None:
    queued(clips=3)
    entries = publisher.load().queue
    publisher._finish(entries[0].id, video_id="VID")
    for _ in range(publisher.MAX_ATTEMPTS):
        publisher._finish(entries[1].id, error="boom")

    publisher.recover_stalled()

    settled = {entry.clip_id: entry.status for entry in publisher.load().queue}
    assert settled["01-clip"] == "posted"
    assert settled["02-clip"] == "failed"


def test_an_edited_title_survives_but_a_posted_one_is_frozen(
    studio_home: Path, queued: Callable[..., list[publisher.Entry]]
) -> None:
    queued(clips=2)
    entries = publisher.load().queue
    publisher._finish(entries[1].id, video_id="VID")

    assert publisher.edit(entries[0].id, {"title": "Better title"}) is not None
    assert publisher.edit(entries[1].id, {"title": "Too late"}) is None
    assert publisher.find(entries[0].id).title == "Better title"  # type: ignore[union-attr]


def test_state_survives_a_field_the_file_has_never_heard_of(
    studio_home: Path, queued: Callable[..., list[publisher.Entry]]
) -> None:
    # The queue outlives releases, so an older or newer file must still open.
    queued(clips=1)
    pipeline.atomic_write_json(publisher.STATE_FILE, {
        "schedule": {"enabled": True, "slots": ["09:00"], "from_the_future": 1},
        "storage": {"delete_clip_after_post": False, "also_new": True},
        "queue": [{"id": "x", "job_id": "job", "clip_id": "c", "file": "f",
                   "title": "t", "description": "d", "unexpected": "field"}],
    })

    board = publisher.load()

    assert board.schedule.slots == ["09:00"]
    assert board.storage.delete_clip_after_post is False
    assert board.queue[0].title == "t"

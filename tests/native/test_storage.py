"""What gets deleted, and what must never be.

Both clean-ups are permanent, so these tests are less about the happy path than
about the refusals: a file the user supplied, a job that failed, a clip whose
upload has not been confirmed.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from studio import pipeline, publisher


def test_a_downloaded_episode_is_removed_with_its_audio(studio_home: Path) -> None:
    url = "https://www.youtube.com/watch?v=AAAAAAAAAAA"
    base = pipeline.MEDIA_DIR / "source" / pipeline.cache_key(url)
    base.parent.mkdir(parents=True)
    base.with_suffix(".mp4").write_bytes(b"\0" * 6000)
    base.with_suffix(".m4a").write_bytes(b"\0" * 4000)

    freed = pipeline.discard_source(url)

    assert freed == 10000
    assert list(base.parent.glob("*")) == []


def test_a_file_the_user_supplied_is_not_ours_to_delete(studio_home: Path) -> None:
    # run_job only calls discard_source for a remote source, and this is the
    # check it relies on.
    mine = studio_home / "my-episode.mp4"
    mine.write_bytes(b"\0" * 999)

    assert pipeline.is_remote(str(mine)) is False
    assert mine.exists()


def test_discarding_a_source_twice_is_harmless(studio_home: Path) -> None:
    url = "https://youtu.be/BBBBBBBBBBB"
    (pipeline.MEDIA_DIR / "source").mkdir(parents=True)

    assert pipeline.discard_source(url) == 0
    assert pipeline.discard_source(url) == 0


def test_a_posted_clip_leaves_no_files_and_no_record(
    studio_home: Path, make_job: Callable[..., pipeline.JobState]
) -> None:
    make_job(clips=2, poster=True)
    directory = pipeline.job_dir("job")

    freed = pipeline.mark_published("job", "01-clip", "VID1", drop_file=True)

    assert freed == 4096 + 256 + 512
    assert not (directory / "01-clip.mp4").exists()
    assert not (directory / "01-clip.jpg").exists()
    assert not (directory / "01-clip-yt.jpg").exists()
    assert [clip.id for clip in pipeline.read_state("job").clips] == ["02-clip"]  # type: ignore[union-attr]


def test_publishing_one_clip_leaves_the_others_alone(
    studio_home: Path, make_job: Callable[..., pipeline.JobState]
) -> None:
    make_job(clips=3)

    pipeline.mark_published("job", "02-clip", "VID", drop_file=True)

    assert (pipeline.job_dir("job") / "01-clip.mp4").exists()
    assert (pipeline.job_dir("job") / "03-clip.mp4").exists()


def test_with_deletion_off_the_clip_stays_and_keeps_its_link(
    studio_home: Path, make_job: Callable[..., pipeline.JobState]
) -> None:
    make_job(clips=1)

    pipeline.mark_published("job", "01-clip", "VID2", drop_file=False)

    state = pipeline.read_state("job")
    assert state is not None
    assert (pipeline.job_dir("job") / "01-clip.mp4").exists()
    assert state.clips[0].video_id == "VID2"


def test_an_upload_that_failed_keeps_its_clip(
    studio_home: Path, queued: Callable[..., list[publisher.Entry]]
) -> None:
    # Only a confirmed video id deletes anything; a failure must never.
    queued(clips=1)
    entry_id = publisher.load().queue[0].id

    publisher._finish(entry_id, error="RuntimeError: network died")

    assert (pipeline.job_dir("job") / "01-clip.mp4").exists()
    assert publisher.find(entry_id).status == "pending"  # type: ignore[union-attr]


def test_a_confirmed_upload_reclaims_the_space(
    studio_home: Path, queued: Callable[..., list[publisher.Entry]]
) -> None:
    queued(clips=1)
    entry_id = publisher.load().queue[0].id

    publisher._finish(entry_id, video_id="VID")

    entry = publisher.find(entry_id)
    assert entry is not None
    assert entry.freed > 0
    assert not (pipeline.job_dir("job") / "01-clip.mp4").exists()


def test_disk_usage_separates_sources_from_clips(
    studio_home: Path, make_job: Callable[..., pipeline.JobState]
) -> None:
    make_job(clips=2)
    (pipeline.MEDIA_DIR / "source").mkdir(parents=True)
    (pipeline.MEDIA_DIR / "source" / "episode.mp4").write_bytes(b"\0" * 10000)

    usage = pipeline.disk_usage()

    assert usage["media"] == 10000
    assert usage["clips"] > 0


def test_job_state_is_written_whole_or_not_at_all(studio_home: Path) -> None:
    # A partially written file was read back as a 500 and killed the browser's
    # polling loop, so the swap has to be atomic.
    target = pipeline.CLIPS_DIR / "state.json"
    pipeline.atomic_write_json(target, {"a": 1})

    pipeline.atomic_write_json(target, {"a": 2})

    assert target.read_text().count('"a"') == 1
    assert list(pipeline.CLIPS_DIR.glob(".state.json.*")) == []

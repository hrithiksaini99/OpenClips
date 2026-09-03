"""What gets deleted, and what must never be.

Both clean-ups are permanent, so these tests are less about the happy path than
about the refusals: a file the user supplied, a job that failed, a clip whose
upload has not been confirmed.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

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


def test_stale_empty_jobs_are_pruned_but_the_newest_is_kept(
    studio_home: Path, make_job: Callable[..., pipeline.JobState]
) -> None:
    # The user does not want a history of failures piling up, but a run that
    # just failed should still be there to look at.
    import time as _time
    for index in range(3):
        state = pipeline.JobState(id=f"failed{index}", source="https://youtu.be/x",
                                  status="error", error="rate limited")
        state.created_at = _time.time() + index  # failed2 is newest
        pipeline.write_state(state)
    make_job(job_id="real", clips=2)

    removed = pipeline.prune_empty_jobs()

    kept = {job.id for job in pipeline.list_jobs()}
    assert removed == 2
    assert kept == {"real", "failed2"}


def test_an_interrupted_job_is_flagged_not_deleted(
    studio_home: Path
) -> None:
    # Its process is gone but its state still says running; without this it
    # vanishes on the next start with no sign of what happened.
    stuck = pipeline.JobState(id="stuck", source="https://youtu.be/x", status="running",
                              progress=0.35, message="Transcribing…")
    pipeline.write_state(stuck)

    assert pipeline.recover_interrupted_jobs() == 1

    recovered = pipeline.read_state("stuck")
    assert recovered is not None
    assert recovered.status == "error"
    assert "cached" in recovered.error


def test_a_job_that_is_genuinely_running_is_left_alone(studio_home: Path) -> None:
    live = pipeline.JobState(id="live", source="https://youtu.be/x", status="running")
    pipeline.write_state(live)

    assert pipeline.recover_interrupted_jobs(active={"live"}) == 0
    assert pipeline.read_state("live").status == "running"  # type: ignore[union-attr]


def test_a_job_still_running_is_never_pruned(studio_home: Path) -> None:
    running = pipeline.JobState(id="live", source="https://youtu.be/x", status="running")
    pipeline.write_state(running)

    assert pipeline.prune_empty_jobs(keep={"live"}) == 0
    assert [job.id for job in pipeline.list_jobs()] == ["live"]


def test_pruning_an_already_clean_list_does_nothing(
    studio_home: Path, make_job: Callable[..., pipeline.JobState]
) -> None:
    make_job(clips=1)

    assert pipeline.prune_empty_jobs() == 0
    assert len(pipeline.list_jobs()) == 1



def test_disk_usage_survives_a_file_vanishing_mid_scan(
    studio_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # yt-dlp deletes fragment files constantly during a download, so a path can
    # disappear between being listed and being stat-ed.
    (pipeline.MEDIA_DIR / "source").mkdir(parents=True)
    real = pipeline.MEDIA_DIR / "source" / "kept.mp4"
    real.write_bytes(b"\0" * 5000)
    ghost = pipeline.MEDIA_DIR / "source" / "frag.part"
    ghost.write_bytes(b"\0" * 100)

    real_stat = Path.stat

    def flaky_stat(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.name == "frag.part":
            raise FileNotFoundError(self)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    assert pipeline.disk_usage()["media"] == 5000

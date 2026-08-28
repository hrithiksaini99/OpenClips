"""Contract tests for the canonical job-kind registry in `application.pipeline`."""

from openclips.application.pipeline import KNOWN_JOB_KINDS, is_known_job_kind


def test_is_known_job_kind_accepts_every_registered_kind() -> None:
    for kind in (
        "ingest_youtube",
        "transcribe",
        "select_clips",
        "render_clip",
        "publish.instagram_reels",
        "publish.youtube_shorts",
    ):
        assert is_known_job_kind(kind) is True


def test_is_known_job_kind_rejects_an_unregistered_legacy_kind() -> None:
    assert is_known_job_kind("legacy_unregistered_kind") is False


def test_registry_matches_the_constants_declared_by_each_producer() -> None:
    """Cross the layer boundary that production `pipeline.py` must never cross.

    `pipeline.py` is a leaf module the coordinators import from, so it spells the
    six kinds out as literals instead of importing them. This test is what keeps
    those literals honest: it reads the constants from the coordinators and the
    `Platform` enum and asserts the registry is exactly their union.
    """
    from openclips.application.clipping import SELECT_CLIPS_JOB_KIND
    from openclips.application.rendering import RENDER_CLIP_JOB_KIND
    from openclips.application.transcription import TRANSCRIBE_JOB_KIND
    from openclips.application.youtube_ingestion import INGEST_YOUTUBE_JOB_KIND
    from openclips.domain.publishing import Platform

    assert {
        INGEST_YOUTUBE_JOB_KIND,
        TRANSCRIBE_JOB_KIND,
        SELECT_CLIPS_JOB_KIND,
        RENDER_CLIP_JOB_KIND,
        Platform.INSTAGRAM_REELS.job_kind,
        Platform.YOUTUBE_SHORTS.job_kind,
    } == KNOWN_JOB_KINDS

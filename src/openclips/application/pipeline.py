"""Shared routing rules for durable pipeline job dispatch."""


KNOWN_JOB_KINDS = frozenset(
    {
        "ingest_youtube",
        "transcribe",
        "select_clips",
        "render_clip",
        "publish.instagram_reels",
        "publish.youtube_shorts",
    }
)


def is_known_job_kind(kind: str) -> bool:
    """Return whether ``kind`` has a registered V1 worker implementation."""
    return kind in KNOWN_JOB_KINDS


def queue_for_job_kind(kind: str) -> str:
    """Map pipeline jobs to the default queue and platform jobs to their own queue."""
    return kind if kind.startswith("publish.") else "default"

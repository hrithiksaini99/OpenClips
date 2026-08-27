"""Shared routing rules for durable pipeline job dispatch."""


def queue_for_job_kind(kind: str) -> str:
    """Map pipeline jobs to the default queue and platform jobs to their own queue."""
    return kind if kind.startswith("publish.") else "default"

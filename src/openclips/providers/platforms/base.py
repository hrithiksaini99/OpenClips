"""Shared platform publisher contract and transport plumbing."""

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


class PublishError(ValueError):
    """Raised when a platform adapter fails to publish media."""


@dataclass(frozen=True)
class PublishRequest:
    """One approved clip ready for upload to a platform."""

    clip_media: Path
    title: str
    description: str = ""
    media_url: str | None = None


@dataclass(frozen=True)
class PublishResult:
    """Platform-assigned identity for published media."""

    external_id: str
    external_url: str


Transport = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class PlatformPublisher:
    """Contract for adapters that publish one clip to one platform."""

    def publish(self, request: PublishRequest) -> PublishResult:
        """Upload the clip media and return the platform identity."""
        raise NotImplementedError


def _stderr_tail(stderr: str, limit: int = 2000) -> str:
    return stderr[-limit:] if len(stderr) > limit else stderr


def require_media(request: PublishRequest) -> None:
    """Validate shared preconditions before any platform call."""
    if not request.clip_media.exists():
        msg = f"Clip media does not exist: {request.clip_media}"
        raise PublishError(msg)
    if not request.title.strip():
        msg = "Publishing requires a non-empty title"
        raise PublishError(msg)


def run_transport(
    transport: Transport,
    argv: Sequence[str],
    *,
    platform: str,
) -> str:
    """Execute a shell-free transport call and return stdout or fail safely."""
    completed = transport(argv)
    if completed.returncode != 0:
        msg = (
            f"{platform} publish failed with code {completed.returncode}: "
            f"{_stderr_tail(completed.stderr)}"
        )
        raise PublishError(msg)
    return completed.stdout

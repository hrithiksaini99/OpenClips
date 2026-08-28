"""Resolve a clip's publicly reachable media URL for URL-based publishers."""

from typing import Protocol
from urllib.parse import urlparse
from uuid import UUID

from openclips.providers.platforms.base import PublishError

_PUBLIC_MEDIA_SETTING = "OPENCLIPS_PUBLIC_MEDIA_BASE_URL"


class PublicMediaUnavailableError(PublishError):
    """Raised when a public media URL is required but not configured."""


class PublicMediaUrlProvider(Protocol):
    """Turns a clip id into a URL a remote platform can fetch the media from."""

    def resolve(self, clip_id: UUID) -> str:
        """Return an absolute, publicly reachable URL for the clip's media."""
        ...


class BaseUrlMediaUrlProvider:
    """Builds media URLs under a configured public base URL."""

    def __init__(self, base_url: str) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            msg = (
                f"{_PUBLIC_MEDIA_SETTING} must be an http(s) URL with a host; "
                f"got {base_url!r}"
            )
            raise ValueError(msg)
        self._base_url = base_url.rstrip("/")

    def resolve(self, clip_id: UUID) -> str:
        return f"{self._base_url}/api/v1/clips/{clip_id}/media"


class UnavailableMediaUrlProvider:
    """Rejects every resolution because no public base URL is configured."""

    def resolve(self, clip_id: UUID) -> str:
        msg = (
            f"No public media URL is configured; set {_PUBLIC_MEDIA_SETTING} to a "
            "publicly reachable base URL to publish through Instagram"
        )
        raise PublicMediaUnavailableError(msg)


def build_media_url_provider(base_url: str) -> PublicMediaUrlProvider:
    """Return a base-URL provider when configured, else the unavailable provider."""
    if not base_url:
        return UnavailableMediaUrlProvider()
    return BaseUrlMediaUrlProvider(base_url)

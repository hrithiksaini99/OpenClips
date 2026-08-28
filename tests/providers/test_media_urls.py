"""Tests for the public media URL provider variants."""

from uuid import uuid4

import pytest

from openclips.providers.media_urls import (
    BaseUrlMediaUrlProvider,
    PublicMediaUnavailableError,
    UnavailableMediaUrlProvider,
    build_media_url_provider,
)


def test_base_url_provider_builds_public_media_url() -> None:
    provider = BaseUrlMediaUrlProvider("https://clips.example")
    clip_id = uuid4()

    assert provider.resolve(clip_id) == f"https://clips.example/api/v1/clips/{clip_id}/media"


def test_base_url_provider_strips_trailing_slash() -> None:
    provider = BaseUrlMediaUrlProvider("https://clips.example/")
    clip_id = uuid4()

    assert provider.resolve(clip_id) == f"https://clips.example/api/v1/clips/{clip_id}/media"


@pytest.mark.parametrize("bad", ["ftp://x", "https://", "", "not-a-url"])
def test_base_url_provider_rejects_invalid_base_url(bad: str) -> None:
    with pytest.raises(ValueError):
        BaseUrlMediaUrlProvider(bad)


def test_unavailable_provider_raises_naming_the_setting() -> None:
    with pytest.raises(PublicMediaUnavailableError, match="OPENCLIPS_PUBLIC_MEDIA_BASE_URL"):
        UnavailableMediaUrlProvider().resolve(uuid4())


def test_build_media_url_provider_empty_returns_unavailable() -> None:
    provider = build_media_url_provider("")

    assert isinstance(provider, UnavailableMediaUrlProvider)


def test_build_media_url_provider_configured_returns_base_url_provider() -> None:
    provider = build_media_url_provider("https://clips.example")

    assert isinstance(provider, BaseUrlMediaUrlProvider)

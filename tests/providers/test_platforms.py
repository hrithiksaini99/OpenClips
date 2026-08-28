"""Contract tests for Instagram Reels and YouTube Shorts adapters."""

import subprocess
from pathlib import Path

import pytest

from openclips.providers.platforms.base import (
    PublishError,
    PublishRequest,
    run_transport,
)
from openclips.providers.platforms.instagram import InstagramReelsPublisher
from openclips.providers.platforms.youtube import YouTubeShortsPublisher


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def media(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"fake-media")
    return path


def _request(media: Path) -> PublishRequest:
    return PublishRequest(
        clip_media=media,
        title="My clip",
        description="desc",
        media_url="https://clips.example/api/v1/clips/abc/media",
    )


def test_instagram_posts_the_provided_media_url_and_never_uses_file_scheme(media: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv) -> subprocess.CompletedProcess[str]:  # type: ignore[no-untyped-def]
        calls.append(list(argv))
        if "/media_publish" in argv[4]:
            return _completed(stdout='{"id": "published-1"}')
        return _completed(stdout='{"id": "container-9"}')

    publisher = InstagramReelsPublisher(
        account_id="acc-1", access_token="tok", transport=runner
    )
    request = PublishRequest(
        clip_media=media,
        title="My clip",
        media_url="https://clips.example/api/v1/clips/xyz/media",
    )
    publisher.publish(request)

    assert '"video_url": "https://clips.example/api/v1/clips/xyz/media"' in calls[0][6]
    assert all("file://" not in part for call in calls for part in call)


def test_instagram_requires_a_public_media_url(media: Path) -> None:
    publisher = InstagramReelsPublisher(
        account_id="acc-1",
        access_token="tok",
        transport=lambda argv: _completed(),
    )
    request = PublishRequest(clip_media=media, title="My clip", media_url=None)

    with pytest.raises(PublishError):
        publisher.publish(request)


def test_transport_runner_raises_publish_error_on_failure() -> None:
    def runner(argv: object) -> subprocess.CompletedProcess[str]:
        return _completed(returncode=1, stderr="boom")

    with pytest.raises(PublishError, match="boom"):
        run_transport(runner, ["curl"], platform="Instagram")


def test_instagram_requires_credentials(tmp_path: Path) -> None:
    publisher = InstagramReelsPublisher(account_id="", access_token="")

    with pytest.raises(PublishError, match="credentials"):
        publisher.publish(_request(tmp_path / "clip.mp4"))


def test_instagram_two_step_container_then_publish(media: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv) -> subprocess.CompletedProcess[str]:  # type: ignore[no-untyped-def]
        calls.append(list(argv))
        if "/media_publish" in argv[4]:
            return _completed(stdout='{"id": "published-1"}')
        return _completed(stdout='{"id": "container-9"}')

    publisher = InstagramReelsPublisher(
        account_id="acc-1", access_token="tok", transport=runner
    )
    result = publisher.publish(_request(media))

    assert result.external_id == "published-1"
    assert result.external_url == "https://www.instagram.com/reel/published-1/"
    assert len(calls) == 2
    assert calls[0][4].endswith("/media")
    assert '"media_type": "REELS"' in calls[0][6]
    assert "My clip" in calls[0][6]


def test_instagram_api_error_is_preserved(media: Path) -> None:
    def runner(argv) -> subprocess.CompletedProcess[str]:  # type: ignore[no-untyped-def]
        return _completed(stdout='{"error": {"message": "rate limited"}}')

    publisher = InstagramReelsPublisher(
        account_id="acc-1", access_token="tok", transport=runner
    )

    with pytest.raises(PublishError, match="rate limited"):
        publisher.publish(_request(media))


def test_instagram_rejects_missing_media() -> None:
    publisher = InstagramReelsPublisher(
        account_id="a",
        access_token="t",
        transport=lambda argv: _completed(),
    )

    with pytest.raises(PublishError, match="does not exist"):
        publisher.publish(PublishRequest(clip_media=Path("/nope/missing.mp4"), title="x"))


def test_youtube_requires_token(tmp_path: Path) -> None:
    publisher = YouTubeShortsPublisher(access_token="", transport=lambda argv: _completed())

    with pytest.raises(PublishError, match="token"):
        publisher.publish(_request(tmp_path / "clip.mp4"))


def test_youtube_uploads_multipart_and_cleans_temp_file(media: Path) -> None:
    captured: dict[str, object] = {}

    def runner(argv) -> subprocess.CompletedProcess[str]:  # type: ignore[no-untyped-def]
        argv_list = list(argv)
        captured["argv"] = argv_list
        body_arg = next(arg for arg in argv_list if arg.startswith("@"))
        captured["body"] = Path(body_arg[1:]).read_text()
        return _completed(stdout='{"id": "vid-77"}')

    publisher = YouTubeShortsPublisher(access_token="yt-tok", transport=runner)
    result = publisher.publish(_request(media))

    argv: list[str] = captured["argv"]  # type: ignore[assignment]
    body: str = str(captured["body"])
    assert result.external_id == "vid-77"
    assert result.external_url == "https://www.youtube.com/shorts/vid-77"
    assert any("uploadType=multipart" in arg for arg in argv)
    assert any(arg.startswith("@") for arg in argv)
    body_arg = next(arg for arg in argv if arg.startswith("@"))
    assert not Path(body_arg[1:]).exists(), "temporary upload body must be removed"
    assert '"snippet"' in body
    assert "My clip" in body


def test_youtube_api_error_is_preserved(media: Path) -> None:
    def runner(argv) -> subprocess.CompletedProcess[str]:  # type: ignore[no-untyped-def]
        return _completed(stdout='{"error": {"code": 403, "message": "quota exceeded"}}')

    publisher = YouTubeShortsPublisher(access_token="tok", transport=runner)

    with pytest.raises(PublishError, match="quota exceeded"):
        publisher.publish(_request(media))


def test_empty_title_is_rejected(tmp_path: Path) -> None:
    publisher = InstagramReelsPublisher(
        account_id="a", access_token="t", transport=lambda argv: _completed()
    )

    with pytest.raises(PublishError, match="title"):
        publisher.publish(PublishRequest(clip_media=tmp_path / "c.mp4", title="  "))

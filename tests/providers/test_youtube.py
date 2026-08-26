import subprocess
from pathlib import Path

import pytest

from openclips.providers.youtube import (
    ProcessRunner,
    UnsupportedMediaLocator,
    YouTubeDownloadError,
    YtDlpDownloader,
    canonicalize_youtube_url,
    extract_youtube_video_id,
)


def make_recording_runner(
    result: subprocess.CompletedProcess[str],
) -> tuple[ProcessRunner, list[list[str]]]:
    """Return a fake runner that records argv calls and replays a fixed result."""
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return result

    return runner, calls


@pytest.mark.parametrize(
    ("url", "expected_id"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("http://youtube.com/watch/?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?v=abc123_ABCd", "abc123_ABCd"),
        ("https://youtu.be/dQw4w9WgXcQ?t=30", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30", "dQw4w9WgXcQ"),
    ],
)
def test_extract_and_canonicalize_accept_supported_variants(url: str, expected_id: str) -> None:
    assert extract_youtube_video_id(url) == expected_id
    assert canonicalize_youtube_url(url) == f"https://www.youtube.com/watch?v={expected_id}"


@pytest.mark.parametrize(
    ("url", "fragment"),
    [
        ("", "scheme"),
        ("https://vimeo.com/98765432109", "host"),
        ("ftp://youtube.com/watch?v=dQw4w9WgXcQ", "scheme"),
        ("https://www.youtube.com/watch", "video id"),
        ("https://www.youtube.com/watch?list=PL123", "video id"),
        ("https://youtu.be/dQw4w9WgXc", "invalid"),
        ("https://youtu.be/dQw4w9WgXcQx", "invalid"),
        ("https://youtu.be/bad*id!123", "invalid"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "unsupported"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "unsupported"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ", "unsupported"),
        ("https://www.youtube.com/feed/history", "path"),
    ],
)
def test_extract_rejects_unsupported_locators(url: str, fragment: str) -> None:
    with pytest.raises(UnsupportedMediaLocator, match=fragment):
        extract_youtube_video_id(url)

    with pytest.raises(UnsupportedMediaLocator, match=fragment):
        canonicalize_youtube_url(url)


def test_download_builds_shell_free_argv_and_runs_once(tmp_path: Path) -> None:
    destination = tmp_path / "video.mp4"
    result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    runner, calls = make_recording_runner(result)
    downloader = YtDlpDownloader(binary="yt-dlp", runner=runner)

    downloader.download("https://youtu.be/dQw4w9WgXcQ?t=30", destination)

    assert len(calls) == 1
    argv = calls[0]
    assert isinstance(argv, list)
    assert argv[0] == "yt-dlp"
    assert "--newline" in argv
    assert "--no-progress" in argv
    output_index = argv.index("-o")
    assert argv[output_index + 1] == str(destination)
    separator_index = argv.index("--")
    assert argv[separator_index + 1] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_download_reports_parsed_progress_values(tmp_path: Path) -> None:
    destination = tmp_path / "video.mp4"
    stdout = "\n".join(
        [
            "[download]   0.0% of 1.00MiB",
            "[download]  42.5% of 1.00MiB",
            "[download] destination already downloaded",
            "[youtube] dQw4w9WgXcQ: Downloading webpage",
            "[download] 100% of 1.00MiB",
        ]
    )
    result = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
    runner, _ = make_recording_runner(result)
    observed: list[float] = []

    YtDlpDownloader(runner=runner).download(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ", destination, observed.append
    )

    assert observed == [0.0, 0.425, 1.0]


def test_download_without_progress_callback_does_not_parse(tmp_path: Path) -> None:
    destination = tmp_path / "video.mp4"
    result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="[download]  42.5% of 1.00MiB", stderr=""
    )
    runner, calls = make_recording_runner(result)

    YtDlpDownloader(runner=runner).download(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ", destination
    )

    assert len(calls) == 1


def test_download_raises_with_stderr_tail_on_failure(tmp_path: Path) -> None:
    destination = tmp_path / "video.mp4"
    result = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="ERROR: unable to download video data"
    )
    runner, _ = make_recording_runner(result)
    downloader = YtDlpDownloader(runner=runner)

    with pytest.raises(YouTubeDownloadError, match="unable to download video data") as excinfo:
        downloader.download("https://www.youtube.com/watch?v=dQw4w9WgXcQ", destination)

    message = str(excinfo.value)
    assert "return code 1" in message
    assert isinstance(excinfo.value, ValueError)


def test_download_truncates_long_stderr_in_error_message(tmp_path: Path) -> None:
    destination = tmp_path / "video.mp4"
    stderr = "A" * 2500 + "END_MARKER"
    result = subprocess.CompletedProcess(args=[], returncode=2, stdout="", stderr=stderr)
    runner, _ = make_recording_runner(result)
    downloader = YtDlpDownloader(runner=runner)

    with pytest.raises(YouTubeDownloadError, match="END_MARKER") as excinfo:
        downloader.download("https://www.youtube.com/watch?v=dQw4w9WgXcQ", destination)

    message = str(excinfo.value)
    assert "return code 2" in message
    assert "A" * 1990 + "END_MARKER" in message
    assert len(message) < len(stderr)


def test_downloader_constructs_without_runner_or_network() -> None:
    downloader = YtDlpDownloader()

    assert isinstance(downloader, YtDlpDownloader)


def test_runner_receives_list_argv_only_once_per_download(tmp_path: Path) -> None:
    destination = tmp_path / "clip.mp4"
    result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    runner, calls = make_recording_runner(result)
    downloader = YtDlpDownloader(binary="/usr/local/bin/yt-dlp", runner=runner)

    downloader.download("https://m.youtube.com/watch?v=abc123_ABCd", destination)

    assert calls == [
        [
            "/usr/local/bin/yt-dlp",
            "--no-progress",
            "--newline",
            "-f",
            "mp4",
            "-o",
            str(destination),
            "--",
            "https://www.youtube.com/watch?v=abc123_ABCd",
        ]
    ]

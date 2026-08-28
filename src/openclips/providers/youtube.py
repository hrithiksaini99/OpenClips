import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


class UnsupportedMediaLocator(ValueError):
    """Raised when a URL is not a supported YouTube video locator."""


class YouTubeDownloadError(ValueError):
    """Raised when the external downloader process fails."""


_ALLOWED_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"})
_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PROGRESS_PATTERN = re.compile(r"^\[download\]\s+(\d+(?:\.\d+)?)%")

_DEFAULT_TIMEOUT_SECONDS = 3600
_STDERR_TAIL_CHARS = 2000

_HTTP_SCHEMES = frozenset({"http", "https"})
_SHORT_PATH_SUFFIXES = ("/shorts/", "/embed/", "/live/")


def extract_youtube_video_id(url: str) -> str:
    """Extract the eleven-character video id from a supported YouTube locator."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in _HTTP_SCHEMES:
        msg = f"URL scheme is not http(s): {url!r}"
        raise UnsupportedMediaLocator(msg)
    hostname = parts.hostname
    if hostname is None or hostname.lower() not in _ALLOWED_HOSTS:
        msg = f"URL host is not a supported YouTube host: {url!r}"
        raise UnsupportedMediaLocator(msg)

    segments = [segment for segment in parts.path.split("/") if segment]
    identifier: str | None
    if hostname.lower().endswith("youtu.be"):
        identifier = segments[0] if segments else None
    else:
        path = parts.path.rstrip("/")
        if path != "/watch":
            if any(suffix in parts.path for suffix in _SHORT_PATH_SUFFIXES):
                msg = f"Shorts, embed, and live locators are unsupported: {url!r}"
                raise UnsupportedMediaLocator(msg)
            msg = f"URL path is not a supported YouTube video locator: {url!r}"
            raise UnsupportedMediaLocator(msg)
        values = parse_qs(parts.query).get("v")
        identifier = values[0] if values else None

    if identifier is None:
        msg = f"URL does not carry a YouTube video id: {url!r}"
        raise UnsupportedMediaLocator(msg)
    if _VIDEO_ID_PATTERN.fullmatch(identifier) is None:
        msg = f"URL carries an invalid YouTube video id: {identifier!r} in {url!r}"
        raise UnsupportedMediaLocator(msg)
    return identifier


def canonicalize_youtube_url(url: str) -> str:
    """Rewrite any supported YouTube locator into its canonical watch URL."""
    return f"https://www.youtube.com/watch?v={extract_youtube_video_id(url)}"


ProcessRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class YtDlpDownloader:
    """Downloads YouTube videos into a destination path through yt-dlp without a shell."""

    def __init__(
        self,
        *,
        binary: str = "yt-dlp",
        runner: ProcessRunner | None = None,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds
        self._runner: ProcessRunner = (
            runner if runner is not None else self._build_default_runner()
        )

    def _build_default_runner(self) -> ProcessRunner:
        timeout = self._timeout_seconds

        def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )

        return run

    def _build_argv(self, url: str, destination: Path) -> list[str]:
        """Assemble the shell-free yt-dlp argv for a canonical watch URL."""
        video_url = f"https://www.youtube.com/watch?v={extract_youtube_video_id(url)}"
        return [
            self._binary,
            "--no-progress",
            "--newline",
            "-f",
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
            "--merge-output-format",
            "mp4",
            "-o",
            str(destination),
            "--",
            video_url,
        ]

    def download(
        self,
        url: str,
        destination: Path,
        progress: Callable[[float], None] | None = None,
    ) -> None:
        """Run yt-dlp for ``url`` into ``destination``, reporting fractional progress."""
        argv = self._build_argv(url, destination)
        completed = self._runner(argv)
        if completed.returncode != 0:
            tail = (completed.stderr or "")[-_STDERR_TAIL_CHARS:]
            msg = f"yt-dlp exited with return code {completed.returncode}; stderr tail: {tail}"
            raise YouTubeDownloadError(msg)
        if progress is None:
            return
        for line in (completed.stdout or "").splitlines():
            match = _PROGRESS_PATTERN.match(line)
            if match is None:
                continue
            progress(float(match.group(1)) / 100.0)

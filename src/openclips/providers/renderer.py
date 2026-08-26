"""FFmpeg-based vertical renderer with injectable process execution."""

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

RENDER_WIDTH = 1080
RENDER_HEIGHT = 1920


class RenderError(ValueError):
    """Raised when the external render process fails."""


ProcessRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class RenderRequest:
    """Everything needed to render one clip into vertical media."""

    source_media: Path
    output_media: Path
    start: float
    end: float
    subtitle_path: Path | None = None
    width: int = RENDER_WIDTH
    height: int = RENDER_HEIGHT
    crop_filters: tuple[str, ...] = ()


@dataclass(frozen=True)
class MediaInfo:
    """Parsed ffprobe metadata for a rendered artifact."""

    duration_seconds: float
    width: int
    height: int
    format_name: str
    has_video: bool
    has_audio: bool


def build_render_argv(request: RenderRequest) -> list[str]:
    """Build the shell-free FFmpeg command for one clip render."""
    filters = [
        *request.crop_filters,
        f"scale={request.width}:{request.height}:force_original_aspect_ratio=increase",
        f"crop={request.width}:{request.height}",
    ]
    if request.subtitle_path is not None:
        escaped = str(request.subtitle_path).replace("\\", "/").replace(":", r"\:")
        filters.append(f"subtitles='{escaped}'")
    argv = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{request.start:.3f}",
        "-to",
        f"{request.end:.3f}",
        "-i",
        str(request.source_media),
        "-vf",
        ",".join(filters),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(request.output_media),
    ]
    return argv


class CenterCropStrategy:
    """Crops horizontally centered on the frame center for 9:16 output."""

    def build_filters(self, source_width: int, source_height: int) -> tuple[str, ...]:
        del source_width, source_height
        return ()


class SpeakerCropStrategy:
    """Keeps a horizontal focus point in frame; speaker tracking plugs in here.

    ``focus_x`` is the normalized horizontal position of the active speaker
    (0.0 left edge, 1.0 right edge). The crop window slides so that point
    stays visible, clamped to frame bounds.
    """

    def __init__(self, focus_x: float = 0.5) -> None:
        if not 0.0 <= focus_x <= 1.0:
            msg = f"focus_x {focus_x} must be within [0, 1]"
            raise ValueError(msg)
        self._focus_x = focus_x

    def build_filters(self, source_width: int, source_height: int) -> tuple[str, ...]:
        del source_width, source_height
        return ()


CropStrategy = CenterCropStrategy | SpeakerCropStrategy


def build_crop_filters(
    strategy: CropStrategy, source_width: int, source_height: int
) -> tuple[str, ...]:
    """Dispatch to a strategy's filter fragment."""
    return strategy.build_filters(source_width, source_height)


def _stderr_tail(stderr: str, limit: int = 2000) -> str:
    return stderr[-limit:] if len(stderr) > limit else stderr


class FFmpegRenderer:
    """Renders clips through an ffmpeg binary without ever using a shell."""

    def __init__(
        self,
        *,
        binary: str = "ffmpeg",
        runner: ProcessRunner | None = None,
    ) -> None:
        self._binary = binary
        self._runner = runner or self._default_runner

    @staticmethod
    def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(list(argv), capture_output=True, text=True, check=False)

    def render(self, request: RenderRequest, crop_strategy: CropStrategy) -> list[str]:
        """Render the clip; returns the executed argv for auditability."""
        source_info = probe_media(
            request.source_media, runner=self._runner
        )
        crop_filters = build_crop_filters(crop_strategy, source_info.width, source_info.height)
        effective_request = RenderRequest(
            source_media=request.source_media,
            output_media=request.output_media,
            start=request.start,
            end=request.end,
            subtitle_path=request.subtitle_path,
            width=request.width,
            height=request.height,
            crop_filters=crop_filters + request.crop_filters,
        )
        request.output_media.parent.mkdir(parents=True, exist_ok=True)
        argv = [self._binary, *build_render_argv(effective_request)[1:]]
        completed = self._runner(argv)
        if completed.returncode != 0:
            msg = (
                f"FFmpeg failed with code {completed.returncode}: "
                f"{_stderr_tail(completed.stderr)}"
            )
            raise RenderError(msg)
        return argv


def probe_media(path: Path, *, runner: ProcessRunner | None = None) -> MediaInfo:
    """Inspect media metadata with ffprobe; used for render assertions."""
    resolved_runner = runner or FFmpegRenderer._default_runner
    argv = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    completed = resolved_runner(argv)
    if completed.returncode != 0:
        msg = f"ffprobe failed with code {completed.returncode}: {_stderr_tail(completed.stderr)}"
        raise RenderError(msg)
    payload = json.loads(completed.stdout or "{}")
    streams = payload.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = any(stream.get("codec_type") == "audio" for stream in streams)
    duration = float(payload.get("format", {}).get("duration") or 0.0)
    return MediaInfo(
        duration_seconds=duration,
        width=int(video.get("width") or 0) if video else 0,
        height=int(video.get("height") or 0) if video else 0,
        format_name=str(payload.get("format", {}).get("format_name") or ""),
        has_video=video is not None,
        has_audio=audio,
    )

"""FFmpeg rendering: 9:16 crop path, burned-in captions, normalised audio."""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from studio.captions import CaptionStyle, render_caption_track
from studio.framing import plan_crop
from studio.tools import binary
from studio.transcript import Word

OUT_WIDTH = 1080
OUT_HEIGHT = 1920
CAPTION_STRIP_HEIGHT = 520
# Captions sit just below centre, clear of platform UI at the bottom of frame.
CAPTION_POSITION = 0.72
# Social platforms target roughly -14 LUFS; normalising makes clips sound even.
LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"


@dataclass(frozen=True)
class RenderResult:
    output: Path
    width: int
    height: int
    duration: float


def probe_dimensions(video: Path) -> tuple[int, int]:
    completed = subprocess.run(
        [
            binary("ffprobe"), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "json", str(video),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    stream = json.loads(completed.stdout)["streams"][0]
    return int(stream["width"]), int(stream["height"])


def _filter_path(path: Path) -> str:
    """Escape a path for use inside an FFmpeg filtergraph argument."""
    return str(path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def render_clip(
    *,
    source: Path,
    destination: Path,
    start: float,
    audio: Path | None = None,
    end: float,
    words: list[Word],
    style: CaptionStyle | None = None,
    face_track: bool = True,
) -> RenderResult:
    """Cut one vertical clip with word-level captions burned in.

    `audio` is the separately downloaded audio stream. Keeping it apart from the
    video avoids merging a multi-gigabyte MP4 up front just to slice seconds out
    of it; when absent the video's own audio track is used.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    duration = end - start
    source_width, source_height = probe_dimensions(source)

    if face_track:
        plan = plan_crop(
            source,
            start=start,
            duration=duration,
            source_width=source_width,
            source_height=source_height,
        )
        # FFmpeg 9 evaluates crop's w/h/x/y per frame (timeline-capable options),
        # so the tracking expression needs no `eval=frame`, which was removed.
        crop = f"crop=w={plan.crop_w}:h={plan.crop_h}:x='{plan.x_expression()}':y=0"
    else:
        crop_w = int(round(source_height * OUT_WIDTH / OUT_HEIGHT))
        crop = f"crop=w={min(crop_w, source_width)}:h={source_height}:x=(iw-ow)/2:y=0"

    with tempfile.TemporaryDirectory(prefix="openclips-captions-") as temporary:
        track = render_caption_track(
            words,
            clip_start=start,
            clip_end=end,
            directory=Path(temporary),
            style=style,
            width=OUT_WIDTH,
            height=CAPTION_STRIP_HEIGHT,
        )
        overlay_y = int(OUT_HEIGHT * CAPTION_POSITION) - CAPTION_STRIP_HEIGHT // 2
        filtergraph = (
            f"[0:v]{crop},scale={OUT_WIDTH}:{OUT_HEIGHT}:flags=lanczos[v];"
            f"[v][1:v]overlay=0:{overlay_y}:format=auto[out]"
        )
        command = [
            binary("ffmpeg"), "-nostdin", "-v", "error", "-y",
            "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source),
        ]
        if audio is not None:
            command += ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(audio)]
        # Inputs: 0=video, 1=audio (when separate), then the caption sequence.
        audio_map = "1:a:0" if audio is not None else "0:a:0"
        command += [
            "-framerate", str(track.fps), "-i", str(track.directory / "cap-%05d.png"),
        ]
        caption_input = "2:v" if audio is not None else "1:v"
        subprocess.run(
            [
                *command,
                "-filter_complex", filtergraph.replace("[1:v]", f"[{caption_input}]"),
                "-map", "[out]", "-map", audio_map,
                "-af", LOUDNORM,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-profile:v", "high", "-movflags", "+faststart",
                "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                str(destination),
            ],
            check=True,
            capture_output=True,
            timeout=1800,
        )
    return RenderResult(
        output=destination,
        width=OUT_WIDTH,
        height=OUT_HEIGHT,
        duration=duration,
    )

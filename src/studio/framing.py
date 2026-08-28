"""Face-aware vertical framing.

A blind centre crop is what made the old clips look amateur: on a two-person
podcast it keeps whatever happens to sit in the middle of the frame and slices
the speaker's head off after a camera cut. Here the clip is sampled with
FFmpeg, faces are detected per sample, and a piecewise-constant crop path is
built that snaps at camera cuts but stays still within a shot.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from studio.tools import binary

SAMPLE_FPS = 2.0
SAMPLE_WIDTH = 480
SEGMENT_SECONDS = 3.0
# Ignore a re-frame smaller than this share of the source width (anti-jitter).
MIN_SHIFT_RATIO = 0.04


@dataclass(frozen=True)
class CropPlan:
    """Crop window width/height in source pixels plus timed horizontal centres."""

    crop_w: int
    crop_h: int
    keyframes: tuple[tuple[float, int], ...]  # (segment start seconds, centre x)

    def x_expression(self) -> str:
        """Build an FFmpeg piecewise expression for the crop's left edge."""
        if len(self.keyframes) == 1:
            return str(self._left(self.keyframes[0][1]))
        expression = str(self._left(self.keyframes[-1][1]))
        for start, center in reversed(self.keyframes[:-1]):
            expression = f"if(lt(t,{start + SEGMENT_SECONDS:.2f}),{self._left(center)},{expression})"
        return expression

    def _left(self, center: int) -> int:
        return max(0, center - self.crop_w // 2)


def _detect_centers(video: Path, start: float, duration: float) -> list[tuple[float, int | None]]:
    """Return (relative time, face centre x in sample pixels) for sampled frames."""
    import cv2  # imported lazily so the module stays importable without OpenCV

    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    samples: list[tuple[float, int | None]] = []
    with tempfile.TemporaryDirectory(prefix="openclips-frames-") as temporary:
        directory = Path(temporary)
        subprocess.run(
            [
                binary("ffmpeg"), "-nostdin", "-v", "error",
                "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(video),
                "-vf", f"fps={SAMPLE_FPS},scale={SAMPLE_WIDTH}:-2",
                "-q:v", "4", str(directory / "f-%05d.jpg"),
            ],
            check=True,
            capture_output=True,
            timeout=600,
        )
        for index, frame_path in enumerate(sorted(directory.glob("f-*.jpg"))):
            image = cv2.imread(str(frame_path), cv2.IMREAD_GRAYSCALE)
            if image is None:
                continue
            faces = cascade.detectMultiScale(image, scaleFactor=1.15, minNeighbors=6, minSize=(46, 46))
            timestamp = index / SAMPLE_FPS
            if len(faces) == 0:
                samples.append((timestamp, None))
                continue
            # Prefer the largest face: the speaker is usually closest to camera.
            x, _y, w, _h = max(faces, key=lambda box: box[2] * box[3])
            samples.append((timestamp, int(x + w / 2)))
    return samples


def plan_crop(
    video: Path,
    *,
    start: float,
    duration: float,
    source_width: int,
    source_height: int,
    target_ratio: float = 9 / 16,
) -> CropPlan:
    """Choose a 9:16 crop window and a timed horizontal path that follows faces."""
    crop_h = source_height
    crop_w = int(round(crop_h * target_ratio))
    if crop_w > source_width:  # already narrower than 9:16
        crop_w = source_width
        crop_h = int(round(crop_w / target_ratio))
    default_center = source_width // 2
    half = crop_w // 2
    limit_low, limit_high = half, max(half, source_width - half)

    try:
        samples = _detect_centers(video, start, duration)
    except Exception:
        samples = []

    scale = source_width / SAMPLE_WIDTH
    segments: list[tuple[float, int]] = []
    segment_count = max(1, int(duration // SEGMENT_SECONDS))
    for index in range(segment_count):
        window_start = index * SEGMENT_SECONDS
        window_end = window_start + SEGMENT_SECONDS
        found = [
            center
            for timestamp, center in samples
            if center is not None and window_start <= timestamp < window_end
        ]
        if found:
            found.sort()
            center = int(found[len(found) // 2] * scale)  # median resists outliers
        elif segments:
            center = segments[-1][1]
        else:
            center = default_center
        segments.append((window_start, min(max(center, limit_low), limit_high)))

    # Collapse micro-adjustments so the frame stays still inside a shot.
    threshold = source_width * MIN_SHIFT_RATIO
    stable: list[tuple[float, int]] = [segments[0]]
    for window_start, center in segments[1:]:
        if abs(center - stable[-1][1]) >= threshold:
            stable.append((window_start, center))
    return CropPlan(crop_w=crop_w, crop_h=crop_h, keyframes=tuple(stable))

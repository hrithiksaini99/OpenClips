"""Build a 1280x720 YouTube thumbnail from a clip.

The per-clip JPEG the renderer already grabs is a 360px frame: fine as a poster
in the results grid, useless as a thumbnail. This makes the real thing — the
clip's own frame, letterboxed against a blurred copy of itself, with the hook
across the bottom.

A Shorts thumbnail never appears in the vertical feed. It appears in search, the
channel grid, the Shorts shelf and subscriptions, which is where a clip is
actually found, so it is worth the one extra Pillow pass.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from studio.captions import _FONT_CANDIDATES
from studio.tools import binary

WIDTH = 1280
HEIGHT = 720
MARGIN = 44
# YouTube rejects a thumbnail over 2 MB; quality 88 lands far below that at
# this size, so the guard below almost never has to do anything.
MAX_BYTES = 2 * 1024 * 1024
_SHADE = (0, 0, 0, 168)


def _font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size)


def _wrap(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, limit: int
) -> list[str]:
    """Greedy wrap on width, measured rather than guessed from character count."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= limit or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale to fill `size`, cropping the overflow rather than squashing."""
    target_w, target_h = size
    scale = max(target_w / image.width, target_h / image.height)
    scaled = image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.LANCZOS,
    )
    left = (scaled.width - target_w) // 2
    top = (scaled.height - target_h) // 2
    return scaled.crop((left, top, left + target_w, top + target_h))


def _backdrop(frame: Image.Image) -> Image.Image:
    """Fit the frame to 1280x720.

    A frame from the original episode is already 16:9 and simply fills the
    canvas. A vertical frame cannot, so a blurred copy of itself fills the sides
    rather than leaving black bars.
    """
    if frame.width / frame.height >= 1.4:
        return _cover(frame, (WIDTH, HEIGHT))
    canvas = _cover(frame, (WIDTH, HEIGHT)).filter(ImageFilter.GaussianBlur(28))
    canvas = Image.blend(canvas, Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0)), 0.45)
    inner_w = max(1, round(HEIGHT * frame.width / frame.height))
    canvas.paste(frame.resize((inner_w, HEIGHT), Image.LANCZOS), ((WIDTH - inner_w) // 2, 0))
    return canvas


def compose(frame: Image.Image, title: str) -> Image.Image:
    """Lay one frame out as a 1280x720 thumbnail with the hook across it."""
    canvas = _backdrop(frame)
    text = " ".join(str(title).split())
    if not text:
        return canvas

    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    limit = WIDTH - MARGIN * 2

    # Shrink until the hook fits in three lines; a four-line thumbnail is
    # unreadable at the size it is actually seen.
    for size in (86, 76, 68, 60, 54):
        font = _font(size)
        lines = _wrap(draw, text, font, limit)
        if len(lines) <= 3:
            break
    else:
        lines = lines[:3]

    line_h = round(size * 1.16)
    block_h = line_h * len(lines)
    top = HEIGHT - MARGIN - block_h

    # A gradient rather than a hard band: the hook stays legible over a busy
    # frame without stamping a black rectangle across half the picture.
    fade_from = max(0, top - MARGIN * 2)
    for y in range(fade_from, HEIGHT):
        share = (y - fade_from) / max(1, HEIGHT - fade_from)
        draw.line([(0, y), (WIDTH, y)], fill=(0, 0, 0, int(_SHADE[3] * share**0.7)))
    for index, line in enumerate(lines):
        y = top + index * line_h
        # A hard outline survives the aggressive recompression YouTube applies.
        draw.text((MARGIN, y), line, font=font, fill=(255, 255, 255, 255),
                  stroke_width=5, stroke_fill=(0, 0, 0, 235))
    return Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")


def grab_frame(video: Path, at: float = 1.0) -> Image.Image:
    """Pull one frame out of a clip at `at` seconds."""
    with tempfile.TemporaryDirectory(prefix="openclips-thumb-") as temporary:
        still = Path(temporary) / "frame.png"
        subprocess.run(
            [
                binary("ffmpeg"), "-nostdin", "-v", "error", "-y",
                "-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "1", str(still),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        with Image.open(still) as image:
            return image.convert("RGB").copy()


def build(video: Path, destination: Path, title: str, *, at: float = 1.0) -> Path:
    """Write the YouTube thumbnail for one clip.

    `video` should be the original episode with `at` inside the clip, not the
    rendered clip: the render has captions burned in, and they read as clutter
    behind the thumbnail's own text.
    """
    thumbnail = compose(grab_frame(video, at), title)
    thumbnail.save(destination, "JPEG", quality=88, optimize=True)
    quality = 88
    while destination.stat().st_size > MAX_BYTES and quality > 40:
        quality -= 12
        thumbnail.save(destination, "JPEG", quality=quality, optimize=True)
    return destination

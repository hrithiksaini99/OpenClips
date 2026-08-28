"""Word-by-word burned-in captions, rendered without libass.

Two problems are solved here. First, timing: the previous pipeline wrote the
source's absolute timestamps into a 21-second clip, so subtitles were scheduled
75 minutes into a file that ended at 0:21 and nothing ever appeared. Every time
here is rebased so t=0 is the clip start.

Second, portability: many FFmpeg builds (including Homebrew's current formula)
ship without libass or freetype, so `ass`, `subtitles` and `drawtext` are all
unavailable. Captions are therefore drawn with Pillow into a transparent strip
and composited by FFmpeg as a plain image sequence, which works on any build.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from studio.transcript import Word

_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/Library/Fonts/Arial Black.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


@dataclass(frozen=True)
class CaptionStyle:
    words_per_line: int = 3
    font_size: int = 108
    active_scale: float = 1.12
    fill: tuple[int, int, int, int] = (255, 255, 255, 255)
    active_fill: tuple[int, int, int, int] = (255, 214, 0, 255)
    stroke: tuple[int, int, int, int] = (0, 0, 0, 255)
    stroke_width: int = 9
    shadow: tuple[int, int, int, int] = (0, 0, 0, 140)
    shadow_offset: int = 6
    uppercase: bool = True
    side_margin: int = 60


@dataclass(frozen=True)
class CaptionTrack:
    directory: Path
    fps: int
    width: int
    height: int
    frames: int


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size)


def _lines(words: list[Word], per_line: int) -> list[list[Word]]:
    return [words[index : index + per_line] for index in range(0, len(words), per_line)]


def _measure(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font, stroke_width=0)
    return box[2] - box[0], box[3] - box[1]


def _render_state(
    line: list[Word],
    active: int | None,
    style: CaptionStyle,
    width: int,
    height: int,
) -> Image.Image:
    """Draw one caption line with a single word highlighted."""
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    size = style.font_size
    while size > 28:
        base = _load_font(size)
        big = _load_font(int(size * style.active_scale))
        widths = [
            _measure(draw, _text(word, style), big if index == active else base)[0]
            for index, word in enumerate(line)
        ]
        space = _measure(draw, " ", base)[0]
        total = sum(widths) + space * (len(line) - 1)
        if total <= width - 2 * style.side_margin:
            break
        size -= 6
    else:
        base, big = _load_font(size), _load_font(size)
        widths = [_measure(draw, _text(w, style), base)[0] for w in line]
        space = _measure(draw, " ", base)[0]
        total = sum(widths) + space * (len(line) - 1)

    cursor = (width - total) / 2
    baseline = height // 2
    for index, word in enumerate(line):
        font = big if index == active else base
        text = _text(word, style)
        fill = style.active_fill if index == active else style.fill
        draw.text(
            (cursor + style.shadow_offset, baseline + style.shadow_offset),
            text, font=font, fill=style.shadow, anchor="lm",
        )
        draw.text(
            (cursor, baseline), text, font=font, fill=fill,
            stroke_width=style.stroke_width, stroke_fill=style.stroke, anchor="lm",
        )
        cursor += widths[index] + space
    return image


def _text(word: Word, style: CaptionStyle) -> str:
    text = word.text.strip()
    return text.upper() if style.uppercase else text


def render_caption_track(
    words: list[Word],
    *,
    clip_start: float,
    clip_end: float,
    directory: Path,
    style: CaptionStyle | None = None,
    fps: int = 12,
    width: int = 1080,
    height: int = 520,
) -> CaptionTrack:
    """Write a transparent PNG sequence of the caption strip for one clip.

    Identical states are rendered once and reused, so a 45-second clip costs a
    few dozen draws rather than one per frame.
    """
    style = style or CaptionStyle()
    directory.mkdir(parents=True, exist_ok=True)
    duration = clip_end - clip_start
    window = [word for word in words if word.end > clip_start and word.start < clip_end]
    lines = _lines(window, style.words_per_line)

    # Each line stays on screen until the next one starts, so there are no gaps.
    spans: list[tuple[float, float, list[Word]]] = []
    for index, line in enumerate(lines):
        start = line[0].start - clip_start
        end = (
            lines[index + 1][0].start - clip_start
            if index + 1 < len(lines)
            else line[-1].end - clip_start + 0.25
        )
        spans.append((start, min(end, duration), line))

    blank = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    cache: dict[tuple[int, int | None], Image.Image] = {}
    total_frames = max(1, int(round(duration * fps)))

    for frame in range(total_frames):
        moment = frame / fps
        image = blank
        for line_index, (start, end, line) in enumerate(spans):
            if start <= moment < end:
                active: int | None = None
                for word_index, word in enumerate(line):
                    if word.start - clip_start <= moment < word.end - clip_start:
                        active = word_index
                        break
                    if word.start - clip_start > moment:
                        break
                key = (line_index, active)
                if key not in cache:
                    cache[key] = _render_state(line, active, style, width, height)
                image = cache[key]
                break
        image.save(directory / f"cap-{frame:05d}.png")

    return CaptionTrack(directory=directory, fps=fps, width=width, height=height, frames=total_frames)

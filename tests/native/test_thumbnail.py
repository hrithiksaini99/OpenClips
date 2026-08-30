"""The thumbnail offered to YouTube.

Only the composition is covered: pulling the frame needs FFmpeg, which CI has no
reason to install, and the part that goes wrong is the layout.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from studio.thumbnail import HEIGHT, MAX_BYTES, WIDTH, compose


def frame(width: int = 1920, height: int = 1080) -> Image.Image:
    return Image.new("RGB", (width, height), (90, 30, 30))


def test_a_widescreen_frame_fills_the_thumbnail() -> None:
    assert compose(frame(), "A hook worth clicking").size == (WIDTH, HEIGHT)


def test_a_vertical_frame_is_letterboxed_not_squashed() -> None:
    # A blurred copy of the frame fills the sides rather than black bars.
    assert compose(frame(1080, 1920), "A hook").size == (WIDTH, HEIGHT)


def test_a_long_hook_still_fits_the_frame() -> None:
    long_title = "Nonverbal autistic children can apparently read Egyptian hieroglyphics unaided"

    assert compose(frame(), long_title).size == (WIDTH, HEIGHT)


def test_an_empty_title_is_not_an_error() -> None:
    assert compose(frame(), "").size == (WIDTH, HEIGHT)


def test_the_file_lands_well_under_youtubes_size_cap(tmp_path: Path) -> None:
    destination = tmp_path / "thumb.jpg"

    compose(frame(), "A hook worth clicking").save(destination, "JPEG", quality=88)

    assert destination.stat().st_size < MAX_BYTES

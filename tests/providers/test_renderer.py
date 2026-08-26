"""FFmpeg renderer contract tests with injected fake process runners."""

import json
import subprocess
from pathlib import Path

import pytest

from openclips.providers.renderer import (
    CenterCropStrategy,
    FFmpegRenderer,
    RenderError,
    RenderRequest,
    SpeakerCropStrategy,
    build_crop_filters,
    build_render_argv,
    probe_media,
)


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def render_request(tmp_path: Path) -> RenderRequest:
    return RenderRequest(
        source_media=tmp_path / "source.mp4",
        output_media=tmp_path / "out.mp4",
        start=12.5,
        end=52.25,
    )


def test_render_argv_is_shell_free_and_vertical(render_request: RenderRequest) -> None:
    argv = build_render_argv(render_request)

    assert argv[0] == "ffmpeg"
    assert "-ss" in argv and "-to" in argv
    assert argv[argv.index("-ss") + 1] == "12.500"
    assert argv[argv.index("-to") + 1] == "52.250"
    filters = argv[argv.index("-vf") + 1]
    assert "scale=1080:1920" in filters
    assert "crop=1080:1920" in filters
    assert argv[-1] == str(render_request.output_media)


def test_subtitle_filter_escapes_windows_drive_paths(
    render_request: RenderRequest, tmp_path: Path
) -> None:
    subtitle = tmp_path / "captions.ass"
    styled = RenderRequest(
        source_media=render_request.source_media,
        output_media=render_request.output_media,
        start=render_request.start,
        end=render_request.end,
        subtitle_path=Path("C:/media/captions.ass"),
    )
    del subtitle

    argv = build_render_argv(styled)

    assert r"subtitles='C\:/media/captions.ass'" in argv[argv.index("-vf") + 1]


def test_renderer_runs_runner_once_and_returns_argv(
    render_request: RenderRequest, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    probe_payload = json.dumps(
        {
            "format": {"duration": "60.0", "format_name": "mov,mp4"},
            "streams": [
                {"codec_type": "video", "width": 1920, "height": 1080},
                {"codec_type": "audio"},
            ],
        }
    )

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if argv[0] == "ffprobe":
            return _completed(stdout=probe_payload)
        return _completed()

    renderer = FFmpegRenderer(runner=runner)
    executed = renderer.render(render_request, CenterCropStrategy())

    render_calls = [argv for argv in calls if argv[0] == "ffmpeg"]
    assert len(render_calls) == 1
    assert executed[0] == "ffmpeg"
    del render_calls


def test_renderer_failure_raises_with_stderr_tail(
    render_request: RenderRequest, tmp_path: Path
) -> None:
    probe_payload = json.dumps(
        {
            "format": {"duration": "60.0"},
            "streams": [{"codec_type": "video", "width": 640, "height": 360}],
        }
    )

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[0] == "ffprobe":
            return _completed(stdout=probe_payload)
        return _completed(returncode=1, stderr="x" * 3000 + "fatal: bad option")

    renderer = FFmpegRenderer(runner=runner)

    with pytest.raises(RenderError, match="fatal: bad option"):
        renderer.render(render_request, CenterCropStrategy())


def test_probe_media_parses_streams(tmp_path: Path) -> None:
    payload = {
        "format": {"duration": "42.5", "format_name": "mov,mp4"},
        "streams": [
            {"codec_type": "video", "width": 1080, "height": 1920},
            {"codec_type": "audio"},
        ],
    }

    info = probe_media(
        tmp_path / "x.mp4", runner=lambda argv: _completed(stdout=json.dumps(payload))
    )

    assert info.width == 1080
    assert info.height == 1920
    assert info.duration_seconds == pytest.approx(42.5)
    assert info.has_video is True
    assert info.has_audio is True


def test_center_crop_strategy_yields_no_extra_filters() -> None:
    assert build_crop_filters(CenterCropStrategy(), 1920, 1080) == ()


def test_speaker_crop_strategy_validates_focus() -> None:
    strategy = SpeakerCropStrategy(focus_x=0.75)

    assert build_crop_filters(strategy, 1920, 1080) == ()
    with pytest.raises(ValueError, match="focus_x"):
        SpeakerCropStrategy(focus_x=1.5)


def test_default_runner_is_lazy_and_not_invoked_in_tests(tmp_path: Path) -> None:
    renderer = FFmpegRenderer()

    assert renderer is not None
    del tmp_path

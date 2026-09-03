"""Slicing the audio, and stitching the pieces back together.

The pieces have to be real short files. Pointing one shared model at ranges of
the whole file with clip_timestamps was the previous approach, and on a
multi-hour episode every worker ground through most of the file and the first
result never arrived.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from studio import pipeline
from studio.tools import binary

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is needed to make a test WAV"
)


def _has_whisper() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def _make_wav(path: Path, seconds: int) -> None:
    subprocess.run(
        [
            binary("ffmpeg"), "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=200:duration={seconds}",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(path),
        ],
        check=True,
        capture_output=True,
    )


def test_a_wav_is_cut_into_fixed_length_pieces_with_offsets(tmp_path: Path) -> None:
    source = tmp_path / "full.wav"
    _make_wav(source, 25)

    pieces = pipeline._split_wav(source, tmp_path / "parts", seconds=10)

    assert [round(offset) for _part, offset in pieces] == [0, 10, 20]
    assert all(part.is_file() and part.stat().st_size > 0 for part, _ in pieces)


def test_a_short_wav_is_one_piece(tmp_path: Path) -> None:
    source = tmp_path / "full.wav"
    _make_wav(source, 6)

    pieces = pipeline._split_wav(source, tmp_path / "parts", seconds=480)

    assert len(pieces) == 1
    assert pieces[0][1] == 0


@pytest.mark.skipif(not _has_whisper(), reason="faster-whisper not installed")
def test_word_timestamps_are_shifted_into_whole_episode_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each piece is transcribed from zero; the offset puts its words back where
    # they belong in the full recording.
    source = tmp_path / "full.wav"
    _make_wav(source, 20)

    class FakeWord:
        def __init__(self, word: str, start: float, end: float) -> None:
            self.word, self.start, self.end, self.probability = word, start, end, 0.9

    class FakeSegment:
        def __init__(self, words: list[FakeWord]) -> None:
            self.words = words

    class FakeModel:
        def transcribe(self, path: str, **_kw: object) -> tuple[list[FakeSegment], object]:
            return [FakeSegment([FakeWord("hi", 1.0, 1.4)])], object()

    def fake_extract(_video: Path, dest: Path) -> float:
        shutil.copy(source, dest)
        return 20.0

    monkeypatch.setattr(pipeline, "_extract_audio", fake_extract)
    monkeypatch.setattr("faster_whisper.WhisperModel", lambda *a, **k: FakeModel())
    monkeypatch.setattr(pipeline, "CHUNK_SECONDS", 10.0)
    monkeypatch.setattr(pipeline, "_shared_model_workers", lambda _mb, n: n)

    words = pipeline.transcribe(source, "small", lambda *_: None, workers=2)

    # Two pieces (0s, 10s), each yielding a word at 1.0s -> 1.0 and 11.0.
    assert sorted(round(w.start, 1) for w in words) == [1.0, 11.0]

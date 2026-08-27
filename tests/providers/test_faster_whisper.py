from collections.abc import Iterator
from pathlib import Path

import pytest

from openclips.domain.transcripts import TranscriptDocument
from openclips.providers.faster_whisper_provider import (
    FasterWhisperProvider,
    normalize_raw_segments,
)
from openclips.providers.transcription import ModelUnavailableError, TranscriptionReadiness


class FakeWord:
    def __init__(self, word: str, start: float, end: float, probability: float) -> None:
        self.word = word
        self.start = start
        self.end = end
        self.probability = probability


class FakeSegment:
    def __init__(
        self, start: float, end: float, text: str, words: list[FakeWord] | None = None
    ) -> None:
        self.start = start
        self.end = end
        self.text = text
        self.words = words or []


class FakeInfo:
    def __init__(self, language: str, duration: float) -> None:
        self.language = language
        self.duration = duration


class FakeModel:
    def __init__(self, segments: list[FakeSegment], info: FakeInfo) -> None:
        self._segments = segments
        self._info = info
        self.calls: list[tuple[str, bool, bool]] = []

    def transcribe(
        self, path: str, *, word_timestamps: bool, vad_filter: bool
    ) -> tuple[Iterator[FakeSegment], FakeInfo]:
        self.calls.append((path, word_timestamps, vad_filter))
        return iter(self._segments), self._info


def test_normalize_maps_words_clamps_and_sorts() -> None:
    raw = [
        FakeSegment(
            3.0,
            6.0,
            " b a ",
            [FakeWord("b", 3.0, 3.5, 0.9), FakeWord("a", 3.5, 9.9, 1.4)],
        ),
        FakeSegment(1.0, 2.0, "first"),
    ]

    segments = normalize_raw_segments(raw, duration=8.0)

    assert [segment.text for segment in segments] == ["first", "b a"]
    assert segments[0].start == 1.0
    assert segments[1].words[1].probability == 1.0
    assert segments[1].words[1].end == 8.0


def test_normalize_drops_empty_segments() -> None:
    raw = [FakeSegment(0.0, 1.0, ""), FakeSegment(1.0, 2.0, "kept")]

    segments = normalize_raw_segments(raw, duration=2.0)

    assert len(segments) == 1
    assert segments[0].text == "kept"


def test_transcribe_builds_document_through_fake_model(tmp_path: Path) -> None:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fake")
    model = FakeModel(
        [FakeSegment(0.0, 1.0, "hello", [FakeWord("hello", 0.0, 1.0, 0.99)])],
        FakeInfo(language="en", duration=1.0),
    )
    provider = FasterWhisperProvider(model_factory=lambda size, device, compute: model)

    document = provider.transcribe(media)

    assert isinstance(document, TranscriptDocument)
    assert document.language == "en"
    assert document.duration == pytest.approx(1.0)
    assert document.full_text == "hello"
    assert model.calls == [(str(media), True, True)]


def test_transcribe_requires_existing_media(tmp_path: Path) -> None:
    provider = FasterWhisperProvider(
        model_factory=lambda size, device, compute: FakeModel([], FakeInfo("en", 0.0))
    )

    with pytest.raises(FileNotFoundError):
        provider.transcribe(tmp_path / "missing.mp4")


def test_readiness_reports_missing_model(tmp_path: Path) -> None:
    provider = FasterWhisperProvider(
        model_root=tmp_path,
        model_factory=lambda size, device, compute: FakeModel([], FakeInfo("en", 0.0)),
    )

    assert provider.is_ready() is False
    with pytest.raises(ModelUnavailableError, match="missing"):
        provider.readiness()

    (tmp_path / "models--Systran--faster-whisper-base").mkdir()
    assert provider.is_ready() is True
    assert "available" in provider.readiness()


def test_model_factory_receives_configuration() -> None:
    received: dict[str, str] = {}

    def factory(size: str, device: str, compute_type: str) -> FakeModel:
        received.update(size=size, device=device, compute_type=compute_type)
        return FakeModel([], FakeInfo("en", 0.0))

    provider = FasterWhisperProvider(
        model_size="small",
        device="cuda",
        compute_type="float16",
        model_factory=factory,
    )
    provider._ensure_model()

    assert received == {"size": "small", "device": "cuda", "compute_type": "float16"}


def test_readiness_reports_download_marker(tmp_path: Path) -> None:
    provider = FasterWhisperProvider(model_root=tmp_path)
    provider.download_marker.parent.mkdir(parents=True, exist_ok=True)
    provider.download_marker.touch()

    assert provider.readiness_state() is TranscriptionReadiness.DOWNLOADING


def test_readiness_reports_available_when_model_directory_exists(tmp_path: Path) -> None:
    provider = FasterWhisperProvider(model_size="base", model_root=tmp_path)
    (tmp_path / "models--Systran--faster-whisper-base").mkdir(parents=True)

    assert provider.readiness_state() is TranscriptionReadiness.AVAILABLE


def test_readiness_reports_missing_without_model_or_marker(tmp_path: Path) -> None:
    provider = FasterWhisperProvider(model_root=tmp_path)

    assert provider.readiness_state() is TranscriptionReadiness.MISSING


def test_available_takes_precedence_over_a_stale_marker(tmp_path: Path) -> None:
    provider = FasterWhisperProvider(model_size="base", model_root=tmp_path)
    (tmp_path / "models--Systran--faster-whisper-base").mkdir(parents=True)
    provider.download_marker.parent.mkdir(parents=True, exist_ok=True)
    provider.download_marker.touch()

    assert provider.readiness_state() is TranscriptionReadiness.AVAILABLE


def test_download_marker_is_model_specific(tmp_path: Path) -> None:
    base = FasterWhisperProvider(model_size="base", model_root=tmp_path)
    large = FasterWhisperProvider(model_size="large-v3", model_root=tmp_path)

    assert base.download_marker != large.download_marker


def test_load_model_creates_marker_during_download_and_clears_it(tmp_path: Path) -> None:
    # The real download path (no model_factory) wraps WhisperModel loading in the
    # marker lifecycle; substitute the loader to observe the marker mid-download.
    provider = FasterWhisperProvider(model_root=tmp_path)
    observed: dict[str, bool] = {}

    def fake_loader() -> object:
        observed["marker_present"] = provider.download_marker.exists()
        return object()

    provider._load_real_model = fake_loader  # type: ignore[method-assign]
    provider._ensure_model()

    assert observed["marker_present"] is True
    assert not provider.download_marker.exists()


def test_load_model_clears_marker_even_when_download_fails(tmp_path: Path) -> None:
    provider = FasterWhisperProvider(model_root=tmp_path)

    def failing_loader() -> object:
        msg = "network down"
        raise RuntimeError(msg)

    provider._load_real_model = failing_loader  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="network down"):
        provider._ensure_model()

    assert not provider.download_marker.exists()

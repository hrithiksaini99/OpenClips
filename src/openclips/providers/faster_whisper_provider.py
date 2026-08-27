"""Local speech-to-text adapter backed by faster-whisper."""

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from openclips.domain.transcripts import (
    TranscriptDocument,
    TranscriptSegment,
    TranscriptWord,
)
from openclips.providers.transcription import (
    ModelUnavailableError,
    TranscriptionProvider,
    TranscriptionReadiness,
)

_DEFAULT_CACHE_ROOT = Path.home() / ".cache" / "huggingface" / "hub"
_MODEL_REPO_TEMPLATE = "models--Systran--faster-whisper-{model_size}"
_DOWNLOAD_MARKER_TEMPLATE = ".openclips-downloading-{model_size}"


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def normalize_raw_segments(raw_segments: Any, duration: float) -> tuple[TranscriptSegment, ...]:
    """Convert provider segment objects into normalized frozen transcript segments."""
    normalized: list[TranscriptSegment] = []
    for raw in raw_segments:
        start = _clamp(float(raw.start), 0.0, duration if duration > 0 else float("inf"))
        end = max(start, float(raw.end))
        text = str(raw.text).strip()
        words = tuple(
            TranscriptWord(
                text=str(word.word).strip(),
                start=_clamp(float(word.start), 0.0, duration),
                end=_clamp(max(float(word.start), float(word.end)), 0.0, duration),
                probability=_clamp(float(word.probability), 0.0, 1.0),
            )
            for word in (getattr(raw, "words", None) or ())
            if str(word.word).strip()
        )
        if not text and not words:
            continue
        normalized.append(TranscriptSegment(start=start, end=end, text=text, words=words))
    normalized.sort(key=lambda segment: (segment.start, segment.end))
    return tuple(normalized)


class FasterWhisperProvider(TranscriptionProvider):
    """Runs faster-whisper locally with word timestamps and deterministic output."""

    def __init__(
        self,
        *,
        model_size: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
        model_root: Path | None = None,
        model_factory: Callable[[str, str, str], Any] | None = None,
    ) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model_root = model_root
        self._model_factory = model_factory
        self._model: Any | None = None
        self._lock = threading.Lock()

    @property
    def download_marker(self) -> Path:
        """Model-specific marker file signalling an in-progress first download."""
        return self._cache_root() / _DOWNLOAD_MARKER_TEMPLATE.format(model_size=self._model_size)

    def is_ready(self) -> bool:
        if self._model is not None:
            return True
        return self._expected_model_path().exists()

    def readiness_state(self) -> TranscriptionReadiness:
        """Report availability from disk: model present, downloading, or missing."""
        if self.is_ready():
            return TranscriptionReadiness.AVAILABLE
        if self.download_marker.exists():
            return TranscriptionReadiness.DOWNLOADING
        return TranscriptionReadiness.MISSING

    def readiness(self) -> str:
        if self.is_ready():
            return f"faster-whisper model '{self._model_size}' available"
        expected = self._expected_model_path()
        msg = (
            f"faster-whisper model '{self._model_size}' is missing at {expected}; "
            "download it once with the 'transcription' extra installed"
        )
        raise ModelUnavailableError(msg)

    def transcribe(self, media_path: Path) -> TranscriptDocument:
        if not media_path.exists():
            msg = f"Media file does not exist: {media_path}"
            raise FileNotFoundError(msg)
        model = self._ensure_model()
        raw_segments, info = model.transcribe(
            str(media_path), word_timestamps=True, vad_filter=True
        )
        segments = normalize_raw_segments(raw_segments, float(info.duration))
        return TranscriptDocument(
            language=str(info.language), duration=float(info.duration), segments=segments
        )

    def _cache_root(self) -> Path:
        return self._model_root if self._model_root is not None else _DEFAULT_CACHE_ROOT

    def _expected_model_path(self) -> Path:
        repo = _MODEL_REPO_TEMPLATE.format(model_size=self._model_size)
        return self._cache_root() / repo

    def _ensure_model(self) -> Any:
        with self._lock:
            if self._model is None:
                self._model = self._load_model()
            return self._model

    def _load_model(self) -> Any:
        if self._model_factory is not None:
            return self._model_factory(self._model_size, self._device, self._compute_type)
        marker = self.download_marker
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)
        try:
            return self._load_real_model()
        finally:
            marker.unlink(missing_ok=True)

    def _load_real_model(self) -> Any:
        """Load the real WhisperModel, downloading it on first use if necessary."""
        from faster_whisper import WhisperModel

        return WhisperModel(self._model_size, device=self._device, compute_type=self._compute_type)

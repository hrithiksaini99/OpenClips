"""Local speech-to-text adapter backed by faster-whisper."""

import subprocess
import tempfile
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
_AUDIO_CHUNK_SECONDS = 600
_AUDIO_EXTRACTION_TIMEOUT_SECONDS = 3600

AudioChunker = Callable[[Path, Path, int], tuple[Path, ...]]


class AudioExtractionError(ValueError):
    """Raised when FFmpeg cannot produce bounded transcription chunks."""


def extract_audio_chunks(
    media_path: Path, directory: Path, chunk_seconds: int
) -> tuple[Path, ...]:
    """Extract mono 16 kHz PCM chunks so memory is bounded for long media."""
    pattern = directory / "chunk-%05d.wav"
    completed = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(media_path),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "segment",
            "-segment_time",
            str(chunk_seconds),
            "-reset_timestamps",
            "1",
            str(pattern),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=_AUDIO_EXTRACTION_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        tail = (completed.stderr or "")[-2000:]
        raise AudioExtractionError(
            f"FFmpeg audio extraction failed with code {completed.returncode}: {tail}"
        )
    chunks = tuple(sorted(directory.glob("chunk-*.wav")))
    if not chunks:
        raise AudioExtractionError("FFmpeg produced no audio chunks")
    return chunks


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


def _offset_segment(segment: TranscriptSegment, offset: float) -> TranscriptSegment:
    return TranscriptSegment(
        start=segment.start + offset,
        end=segment.end + offset,
        text=segment.text,
        words=tuple(
            TranscriptWord(
                text=word.text,
                start=word.start + offset,
                end=word.end + offset,
                probability=word.probability,
            )
            for word in segment.words
        ),
    )


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
        chunker: AudioChunker = extract_audio_chunks,
    ) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model_root = model_root
        self._model_factory = model_factory
        self._chunker = chunker
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
        all_segments: list[TranscriptSegment] = []
        language = "unknown"
        offset = 0.0
        with tempfile.TemporaryDirectory(prefix="openclips-transcribe-") as temporary:
            chunks = self._chunker(media_path, Path(temporary), _AUDIO_CHUNK_SECONDS)
            for chunk in chunks:
                raw_segments, info = model.transcribe(
                    str(chunk),
                    word_timestamps=True,
                    vad_filter=True,
                    beam_size=1,
                    best_of=1,
                )
                duration = float(info.duration)
                if language == "unknown":
                    language = str(info.language)
                all_segments.extend(
                    _offset_segment(segment, offset)
                    for segment in normalize_raw_segments(raw_segments, duration)
                )
                offset += duration
        return TranscriptDocument(language=language, duration=offset, segments=tuple(all_segments))

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

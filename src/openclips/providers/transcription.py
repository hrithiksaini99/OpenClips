"""Transcription provider contract implemented by local and fake adapters."""

from enum import StrEnum
from pathlib import Path

from openclips.domain.transcripts import TranscriptDocument


class ModelUnavailableError(ValueError):
    """Raised when the configured local transcription model cannot be located."""


class TranscriptionReadiness(StrEnum):
    """Lifecycle of the local transcription model's on-disk availability."""

    MISSING = "missing"
    DOWNLOADING = "downloading"
    AVAILABLE = "available"


class TranscriptionProvider:
    """Contract for providers that turn media into a normalized transcript."""

    def is_ready(self) -> bool:
        """Return whether the provider can transcribe right now."""
        raise NotImplementedError

    def readiness_state(self) -> TranscriptionReadiness:
        """Report the model's availability without blocking or downloading."""
        return (
            TranscriptionReadiness.AVAILABLE
            if self.is_ready()
            else TranscriptionReadiness.MISSING
        )

    def readiness(self) -> str:
        """Return an actionable human-readable readiness description."""
        raise NotImplementedError

    def transcribe(self, media_path: Path) -> TranscriptDocument:
        """Transcribe the media file into a normalized transcript document."""
        raise NotImplementedError

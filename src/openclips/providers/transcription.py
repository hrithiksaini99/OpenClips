"""Transcription provider contract implemented by local and fake adapters."""

from pathlib import Path

from openclips.domain.transcripts import TranscriptDocument


class ModelUnavailableError(ValueError):
    """Raised when the configured local transcription model cannot be located."""


class TranscriptionProvider:
    """Contract for providers that turn media into a normalized transcript."""

    def is_ready(self) -> bool:
        """Return whether the provider can transcribe right now."""
        raise NotImplementedError

    def readiness(self) -> str:
        """Return an actionable human-readable readiness description."""
        raise NotImplementedError

    def transcribe(self, media_path: Path) -> TranscriptDocument:
        """Transcribe the media file into a normalized transcript document."""
        raise NotImplementedError

from collections.abc import Iterable
from pathlib import PurePosixPath

from openclips.application.ingestion import IngestionCoordinator
from openclips.domain.sources import SourceKind
from openclips.infrastructure.models import SourceAssetRecord

_ALLOWED_SUFFIXES = frozenset({".mp4", ".mov"})


class UnsupportedUploadError(ValueError):
    """Raised when an upload has an extension outside the supported set."""


def _display_name(filename: str) -> str:
    component = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if component in ("", ".", "..") or not component.strip():
        msg = f"Unsupported upload filename: {filename!r}"
        raise ValueError(msg)
    return component


class LocalUploadIngestor:
    """Validates and registers local video uploads through the ingestion coordinator."""

    def __init__(self, coordinator: IngestionCoordinator) -> None:
        self._coordinator = coordinator

    def ingest(self, filename: str, chunks: Iterable[bytes]) -> SourceAssetRecord:
        """Validate the upload extension and register its stream for ingestion."""
        display_name = _display_name(filename)
        suffix = PurePosixPath(display_name).suffix.lower()
        if suffix not in _ALLOWED_SUFFIXES:
            allowed = ", ".join(sorted(_ALLOWED_SUFFIXES))
            msg = f"Unsupported upload extension {suffix!r}: allowed extensions are {allowed}"
            raise UnsupportedUploadError(msg)
        return self._coordinator.register(
            source_kind=SourceKind.LOCAL_UPLOAD,
            original_locator=filename,
            display_name=display_name,
            chunks=chunks,
        )

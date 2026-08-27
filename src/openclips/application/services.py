"""Shared singletons wired from settings for API and worker composition."""

from dataclasses import dataclass

from openclips.config import Settings
from openclips.domain.captions import CaptionStyle, get_template
from openclips.domain.selection import SelectionBounds
from openclips.infrastructure.media_storage import MediaStorage
from openclips.providers.llm import ClipRefiner, HeuristicClipRefiner
from openclips.providers.renderer import (
    RENDER_HEIGHT,
    RENDER_WIDTH,
    CenterCropStrategy,
    FFmpegRenderer,
)
from openclips.providers.transcription import TranscriptionProvider
from openclips.providers.youtube import YtDlpDownloader


@dataclass(frozen=True)
class AppServices:
    """Collaborators shared across requests and worker handlers."""

    storage: MediaStorage
    bounds: SelectionBounds
    style: CaptionStyle
    refiner: ClipRefiner
    transcription_provider: TranscriptionProvider
    renderer: FFmpegRenderer
    crop_strategy: CenterCropStrategy
    downloader: YtDlpDownloader
    max_upload_bytes: int
    width: int = RENDER_WIDTH
    height: int = RENDER_HEIGHT


def build_services(settings: Settings) -> AppServices:
    """Construct the service singletons without touching external systems."""
    from openclips.providers.faster_whisper_provider import FasterWhisperProvider

    return AppServices(
        storage=MediaStorage(settings.media_root),
        bounds=SelectionBounds(
            max_clips=settings.max_clips,
            min_duration_seconds=settings.min_clip_seconds,
            max_duration_seconds=settings.max_clip_seconds,
        ),
        style=get_template(settings.caption_template),
        refiner=HeuristicClipRefiner(),
        transcription_provider=FasterWhisperProvider(
            model_size=settings.transcription_model_size,
            device=settings.transcription_device,
            compute_type=settings.transcription_compute_type,
        ),
        renderer=FFmpegRenderer(),
        crop_strategy=CenterCropStrategy(),
        downloader=YtDlpDownloader(),
        max_upload_bytes=settings.max_upload_bytes,
        width=settings.render_width,
        height=settings.render_height,
    )

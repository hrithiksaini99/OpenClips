"""Worker process: claims queued jobs and dispatches them to registered handlers."""

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from openclips.application.clipping import SELECT_CLIPS_JOB_KIND, ClipSelectionCoordinator
from openclips.application.dispatch import OutboxRelay
from openclips.application.health import make_database_probe
from openclips.application.rendering import RENDER_CLIP_JOB_KIND, RenderCoordinator
from openclips.application.transcription import TRANSCRIBE_JOB_KIND, TranscriptionCoordinator
from openclips.application.youtube_ingestion import (
    INGEST_YOUTUBE_JOB_KIND,
    YouTubeIngestionCoordinator,
)
from openclips.config import Settings
from openclips.domain.captions import CaptionStyle, get_template
from openclips.domain.jobs import JobEvent, JobStatus
from openclips.domain.selection import SelectionBounds
from openclips.infrastructure.db import make_engine
from openclips.infrastructure.media_storage import MediaStorage
from openclips.infrastructure.models import JobRecord
from openclips.infrastructure.queue import InMemoryJobQueue, RedisJobQueue
from openclips.infrastructure.repositories import (
    ClipRepository,
    JobRepository,
    SourceRepository,
    TranscriptRepository,
)
from openclips.providers.faster_whisper_provider import FasterWhisperProvider
from openclips.providers.llm import ClipRefiner, HeuristicClipRefiner
from openclips.providers.renderer import FFmpegRenderer
from openclips.providers.youtube import YtDlpDownloader

if TYPE_CHECKING:
    from redis import Redis

logger = logging.getLogger(__name__)

QUEUE_NAMES = ("default", "publish.instagram_reels", "publish.youtube_shorts")
JobQueue = InMemoryJobQueue | RedisJobQueue
Handler = Callable[[Session, JobRecord], None]


class UnknownJobKindError(ValueError):
    """Raised when a claimed job references an unregistered kind."""


def _failure_message(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def make_transcribe_handler(provider: FasterWhisperProvider, storage: MediaStorage) -> Handler:
    """Build a handler that executes transcription jobs inside a worker session."""

    def handle(session: Session, job: JobRecord) -> None:
        coordinator = TranscriptionCoordinator(
            sources=SourceRepository(session),
            transcripts=TranscriptRepository(session),
            jobs=JobRepository(session),
            provider=provider,
            storage=storage,
        )
        coordinator.run(job)

    return handle


def make_select_clips_handler(refiner: ClipRefiner, bounds: SelectionBounds) -> Handler:
    """Build a handler that executes clip-selection jobs inside a worker session."""

    def handle(session: Session, job: JobRecord) -> None:
        coordinator = ClipSelectionCoordinator(
            sources=SourceRepository(session),
            transcripts=TranscriptRepository(session),
            clips=ClipRepository(session),
            jobs=JobRepository(session),
            refiner=refiner,
            bounds=bounds,
        )
        coordinator.run(job)

    return handle


def make_render_handler(
    renderer: FFmpegRenderer,
    storage: MediaStorage,
    style: CaptionStyle,
    width: int,
    height: int,
) -> Handler:
    """Build a handler that executes render jobs inside a worker session."""

    def handle(session: Session, job: JobRecord) -> None:
        coordinator = RenderCoordinator(
            clips=ClipRepository(session),
            sources=SourceRepository(session),
            transcripts=TranscriptRepository(session),
            jobs=JobRepository(session),
            renderer=renderer,
            storage=storage,
            style=style,
            width=width,
            height=height,
        )
        coordinator.run(job)

    return handle


def make_youtube_ingest_handler(downloader: YtDlpDownloader, storage: MediaStorage) -> Handler:
    """Build a handler that downloads and promotes YouTube sources in the worker."""

    def handle(session: Session, job: JobRecord) -> None:
        coordinator = YouTubeIngestionCoordinator(
            sources=SourceRepository(session),
            jobs=JobRepository(session),
            storage=storage,
            downloader=downloader,
        )
        coordinator.run(job)

    return handle


def build_handlers(settings: Settings, storage: MediaStorage) -> dict[str, Handler]:
    """Register one handler per supported job kind."""
    provider = FasterWhisperProvider(
        model_size=settings.transcription_model_size,
        device=settings.transcription_device,
        compute_type=settings.transcription_compute_type,
    )
    bounds = SelectionBounds(
        max_clips=settings.max_clips,
        min_duration_seconds=settings.min_clip_seconds,
        max_duration_seconds=settings.max_clip_seconds,
    )
    handlers = {
        INGEST_YOUTUBE_JOB_KIND: make_youtube_ingest_handler(YtDlpDownloader(), storage),
        TRANSCRIBE_JOB_KIND: make_transcribe_handler(provider, storage),
        SELECT_CLIPS_JOB_KIND: make_select_clips_handler(HeuristicClipRefiner(), bounds),
        RENDER_CLIP_JOB_KIND: make_render_handler(
            FFmpegRenderer(),
            storage,
            get_template(settings.caption_template),
            settings.render_width,
            settings.render_height,
        ),
    }
    handlers.update(
        make_publish_handlers(
            instagram_account_id=settings.instagram_account_id,
            instagram_access_token=settings.instagram_access_token,
            youtube_access_token=settings.youtube_access_token,
            storage=storage,
        )
    )
    return handlers


def make_publish_handlers(
    *,
    instagram_account_id: str,
    instagram_access_token: str,
    youtube_access_token: str,
    storage: MediaStorage,
) -> dict[str, Handler]:
    """Register independent publish handlers per platform queue."""

    from openclips.application.publishing import ScheduleCoordinator
    from openclips.domain.publishing import Platform
    from openclips.infrastructure.repositories import PublicationRepository
    from openclips.providers.platforms.base import PlatformPublisher
    from openclips.providers.platforms.instagram import InstagramReelsPublisher
    from openclips.providers.platforms.youtube import YouTubeShortsPublisher

    publisher_map: dict[Platform, PlatformPublisher] = {
        Platform.INSTAGRAM_REELS: InstagramReelsPublisher(
            account_id=instagram_account_id, access_token=instagram_access_token
        ),
        Platform.YOUTUBE_SHORTS: YouTubeShortsPublisher(access_token=youtube_access_token),
    }

    def _make(platform: Platform) -> Handler:
        def handle(session: Session, job: JobRecord) -> None:
            coordinator = ScheduleCoordinator(
                clips=ClipRepository(session),
                publications=PublicationRepository(session),
                jobs=JobRepository(session),
                publishers={platform: publisher_map[platform]},
                storage=storage,
            )
            coordinator.run(job)

        return handle

    return {platform.job_kind: _make(platform) for platform in Platform}


def recover_startup_state(
    *,
    session_factory: sessionmaker[Session],
    queue: JobQueue,
    queue_names: tuple[str, ...] = QUEUE_NAMES,
) -> int:
    """Restore unacknowledged receipts, then redispatch jobs left running after a crash."""
    for queue_name in queue_names:
        restored = queue.restore_processing(queue_name)
        if restored:
            logger.info("Restored %s unacknowledged messages to %s", restored, queue_name)
    with session_factory() as session:
        recovered = JobRepository(session).recover_running()
        session.commit()
    if recovered:
        logger.info("Recovered %s running jobs for durable redis dispatch", len(recovered))
    return len(recovered)


def process_once(
    *,
    session_factory: sessionmaker[Session],
    handlers: dict[str, Handler],
    queue: JobQueue,
    queue_names: tuple[str, ...] = QUEUE_NAMES,
    claim_timeout_seconds: float = 1.0,
) -> bool:
    """Claim and process at most one job; return True when a payload was handled."""
    for queue_name in queue_names:
        receipt = queue.claim(queue_name, timeout_seconds=claim_timeout_seconds)
        if receipt is None:
            continue
        _process_payload(UUID(receipt.payload), session_factory, handlers)
        queue.ack(receipt)
        return True
    return False


def _process_payload(
    payload: UUID,
    session_factory: sessionmaker[Session],
    handlers: dict[str, Handler],
) -> None:
    with session_factory() as session:
        jobs = JobRepository(session)
        job = jobs.get_for_update(payload)
        if job is None:
            logger.warning("Ignoring unknown job id %s claimed from the queue", payload)
            session.commit()
            return
        if job.status is not JobStatus.QUEUED:
            logger.info(
                "Ignoring duplicate queue message for job %s in status %s", payload, job.status
            )
            session.commit()
            return
        handler = handlers.get(job.kind)
        if handler is None:
            message = f"UnknownJobKindError: {job.kind}"
            logger.error("Job %s references unregistered kind: %s", payload, job.kind)
            _fail_job(session_factory, payload, message)
            return
        try:
            jobs.transition(job.id, JobEvent.START)
            handler(session, job)
            jobs.transition(job.id, JobEvent.SUCCEED)
            session.commit()
            logger.info("Job %s (%s) succeeded", job.id, job.kind)
        except Exception as error:
            session.rollback()
            message = _failure_message(error)
            logger.exception("Job %s (%s) failed: %s", job.id, job.kind, message)
            _fail_job(session_factory, payload, message)


def _fail_job(session_factory: sessionmaker[Session], job_id: UUID, message: str) -> None:
    with session_factory() as session:
        jobs = JobRepository(session)
        record = jobs.get(job_id)
        if record is None:
            return
        if record.status is JobStatus.QUEUED:
            jobs.transition(job_id, JobEvent.START)
        jobs.transition(job_id, JobEvent.FAIL, error=message)
        session.commit()


def run() -> None:
    """Run the OpenClips worker until interrupted; used by the console script."""
    settings = Settings()
    logging.basicConfig(level=settings.log_level.upper())
    engine = make_engine(settings.database_url)
    session_factory = sessionmaker(bind=engine)
    storage = MediaStorage(settings.media_root)
    handlers = build_handlers(settings, storage)

    import redis

    client: Redis = redis.Redis.from_url(settings.redis_url)
    queue = RedisJobQueue(client)
    relay = OutboxRelay(
        session_factory=session_factory,
        queue=queue,
        clock=lambda: datetime.now(UTC),
        batch_size=settings.outbox_batch_size,
        backoff_cap_seconds=settings.outbox_backoff_cap_seconds,
    )

    probe = make_database_probe(engine)
    probe()
    recover_startup_state(session_factory=session_factory, queue=queue)
    logger.info(
        "OpenClips worker started with concurrency=%s queues=%s",
        settings.worker_concurrency,
        ",".join(QUEUE_NAMES),
    )
    try:
        while True:
            relay.dispatch_once()
            handled = process_once(session_factory=session_factory, handlers=handlers, queue=queue)
            if not handled:
                time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info("OpenClips worker stopped")

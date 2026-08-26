"""Worker process: claims queued jobs and dispatches them to registered handlers."""

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from openclips.application.health import make_database_probe
from openclips.application.transcription import TRANSCRIBE_JOB_KIND, TranscriptionCoordinator
from openclips.config import Settings
from openclips.domain.jobs import JobEvent, JobStatus
from openclips.infrastructure.db import make_engine
from openclips.infrastructure.media_storage import MediaStorage
from openclips.infrastructure.models import JobRecord
from openclips.infrastructure.queue import InMemoryJobQueue, RedisJobQueue
from openclips.infrastructure.repositories import (
    JobRepository,
    SourceRepository,
    TranscriptRepository,
)
from openclips.providers.faster_whisper_provider import FasterWhisperProvider

if TYPE_CHECKING:
    from redis import Redis

logger = logging.getLogger(__name__)

QUEUE_NAMES = ("default",)
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


def build_handlers(settings: Settings, storage: MediaStorage) -> dict[str, Handler]:
    """Register one handler per supported job kind."""
    provider = FasterWhisperProvider(
        model_size=settings.transcription_model_size,
        device=settings.transcription_device,
        compute_type=settings.transcription_compute_type,
    )
    return {TRANSCRIBE_JOB_KIND: make_transcribe_handler(provider, storage)}


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
        payload = queue.claim(queue_name, timeout_seconds=claim_timeout_seconds)
        if payload is None:
            continue
        _process_payload(UUID(payload), session_factory, handlers)
        return True
    return False


def _process_payload(
    payload: UUID,
    session_factory: sessionmaker[Session],
    handlers: dict[str, Handler],
) -> None:
    with session_factory() as session:
        jobs = JobRepository(session)
        job = jobs.get(payload)
        if job is None:
            logger.warning("Ignoring unknown job id %s claimed from the queue", payload)
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

    probe = make_database_probe(engine)
    probe()
    logger.info(
        "OpenClips worker started with concurrency=%s queues=%s",
        settings.worker_concurrency,
        ",".join(QUEUE_NAMES),
    )
    try:
        while True:
            handled = process_once(session_factory=session_factory, handlers=handlers, queue=queue)
            if not handled:
                time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info("OpenClips worker stopped")

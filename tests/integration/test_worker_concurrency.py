"""PostgreSQL-backed contracts for bounded worker and stage concurrency."""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import BoundedSemaphore, Event, Lock, Thread

import pytest
from sqlalchemy.orm import Session, sessionmaker

from openclips import worker
from openclips.config import Settings
from openclips.domain.jobs import JobStatus
from openclips.infrastructure.models import JobRecord
from openclips.infrastructure.queue import InMemoryJobQueue
from openclips.infrastructure.repositories import JobRepository

pytestmark = pytest.mark.integration

WORKER_CONCURRENCY = 2
EXTRA_JOBS = 3


@dataclass
class _ConcurrencyProbe:
    total: int
    stop_event: Event
    expected_overlap: int
    hold_at_barrier: bool = False
    lock: Lock = field(default_factory=Lock)
    barrier_reached: Event = field(default_factory=Event)
    release_barrier: Event = field(default_factory=Event)
    active: int = 0
    max_active: int = 0
    completed: int = 0

    def __call__(self, session: Session, job: JobRecord) -> None:
        del session, job
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active >= self.expected_overlap:
                self.barrier_reached.set()
        try:
            if self.hold_at_barrier:
                self.release_barrier.wait(timeout=3)
            else:
                time.sleep(0.02)
        finally:
            with self.lock:
                self.active -= 1
                self.completed += 1
                if self.completed == self.total:
                    self.stop_event.set()


def _enqueue_jobs(
    session_factory: sessionmaker[Session],
    queue: InMemoryJobQueue,
    *,
    kind: str,
    count: int,
) -> list[object]:
    with session_factory() as session:
        jobs = JobRepository(session)
        records = [jobs.create(kind) for _ in range(count)]
        job_ids = [record.id for record in records]
        session.commit()
    for job_id in job_ids:
        queue.enqueue("default", str(job_id))
    return job_ids


def _run_in_thread(
    *,
    session_factory: sessionmaker[Session],
    handlers: dict[str, worker.Handler],
    queue: InMemoryJobQueue,
    stop_event: Event,
    concurrency: int,
) -> tuple[Thread, list[BaseException], ThreadPoolExecutor]:
    assert hasattr(worker, "run_claim_loop"), "bounded worker claim loop is missing"
    pool = ThreadPoolExecutor(max_workers=concurrency)
    errors: list[BaseException] = []

    def target() -> None:
        try:
            worker.run_claim_loop(
                session_factory=session_factory,
                handlers=handlers,
                queue=queue,
                pool=pool,
                permits=BoundedSemaphore(concurrency),
                stop_event=stop_event,
            )
        except BaseException as error:
            errors.append(error)
            stop_event.set()

    thread = Thread(target=target)
    thread.start()
    return thread, errors, pool


def _assert_jobs_succeeded(
    session_factory: sessionmaker[Session], job_ids: list[object]
) -> None:
    with session_factory() as session:
        statuses = [JobRepository(session).get(job_id).status for job_id in job_ids]  # type: ignore[arg-type, union-attr]
    assert statuses == [JobStatus.SUCCEEDED] * len(job_ids)


def test_claim_loop_never_has_more_than_worker_concurrency_in_flight(
    session_factory: sessionmaker[Session],
) -> None:
    total = WORKER_CONCURRENCY + EXTRA_JOBS
    queue = InMemoryJobQueue()
    stop_event = Event()
    probe = _ConcurrencyProbe(
        total=total,
        stop_event=stop_event,
        expected_overlap=WORKER_CONCURRENCY,
        hold_at_barrier=True,
    )
    job_ids = _enqueue_jobs(session_factory, queue, kind="select_clips", count=total)
    thread, errors, pool = _run_in_thread(
        session_factory=session_factory,
        handlers={"select_clips": probe},
        queue=queue,
        stop_event=stop_event,
        concurrency=WORKER_CONCURRENCY,
    )
    try:
        assert probe.barrier_reached.wait(timeout=2)
        assert queue.processing_depth("default") == WORKER_CONCURRENCY
        assert queue.depth("default") == EXTRA_JOBS
        probe.release_barrier.set()
        thread.join(timeout=5)
    finally:
        probe.release_barrier.set()
        stop_event.set()
        thread.join(timeout=5)
        pool.shutdown(wait=True)

    assert not thread.is_alive()
    assert errors == []
    assert probe.max_active <= WORKER_CONCURRENCY
    assert queue.depth("default") == 0
    assert queue.processing_depth("default") == 0
    _assert_jobs_succeeded(session_factory, job_ids)


def test_render_stage_limit_serializes_jobs_inside_wider_worker_pool(
    session_factory: sessionmaker[Session],
) -> None:
    assert hasattr(worker, "run_claim_loop"), "bounded worker claim loop is missing"
    concurrency_module = __import__(
        "openclips.application.concurrency", fromlist=["StageLimiter"]
    )
    total = WORKER_CONCURRENCY + EXTRA_JOBS
    settings = Settings(
        _env_file=None,
        worker_concurrency=WORKER_CONCURRENCY,
        max_concurrent_renders=1,
    )
    limiter = concurrency_module.StageLimiter(
        {"render_clip": settings.max_concurrent_renders}
    )
    queue = InMemoryJobQueue()
    stop_event = Event()
    probe = _ConcurrencyProbe(total=total, stop_event=stop_event, expected_overlap=1)

    def render_handler(session: Session, job: JobRecord) -> None:
        with limiter.limit("render_clip"):
            probe(session, job)

    job_ids = _enqueue_jobs(session_factory, queue, kind="render_clip", count=total)
    thread, errors, pool = _run_in_thread(
        session_factory=session_factory,
        handlers={"render_clip": render_handler},
        queue=queue,
        stop_event=stop_event,
        concurrency=WORKER_CONCURRENCY,
    )
    try:
        thread.join(timeout=5)
    finally:
        stop_event.set()
        thread.join(timeout=5)
        pool.shutdown(wait=True)

    assert not thread.is_alive()
    assert errors == []
    assert probe.max_active == 1
    assert queue.depth("default") == 0
    assert queue.processing_depth("default") == 0
    _assert_jobs_succeeded(session_factory, job_ids)

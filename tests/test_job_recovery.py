"""Durable job retry, restart recovery, and duplicate delivery behavior."""

import os
import threading
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from openclips.application.pipeline import queue_for_job_kind
from openclips.domain.jobs import JobEvent, JobStatus
from openclips.infrastructure.models import Base, OutboxRecord
from openclips.infrastructure.queue import InMemoryJobQueue
from openclips.infrastructure.repositories import JobRepository
from openclips.worker import _process_payload, recover_startup_state


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_queue_for_job_kind_keeps_platform_queue_and_defaults_pipeline() -> None:
    assert queue_for_job_kind("publish.instagram_reels") == "publish.instagram_reels"
    assert queue_for_job_kind("publish.youtube_shorts") == "publish.youtube_shorts"
    assert queue_for_job_kind("transcribe") == "default"


def test_recover_running_requeues_without_new_attempt_and_creates_dispatch() -> None:
    factory = _session_factory()
    with factory() as session:
        jobs = JobRepository(session)
        running = jobs.create("transcribe", payload=str(uuid4()))
        jobs.transition(running.id, JobEvent.START)
        running_id = running.id
        session.commit()

    with factory() as session:
        recovered = JobRepository(session).recover_running()
        recovered_ids = [job.id for job in recovered]
        recovered_statuses = [job.status for job in recovered]
        recovered_attempts = [job.attempts for job in recovered]
        recovered_errors = [job.error for job in recovered]
        session.commit()

    assert recovered_ids == [running_id]
    assert recovered_statuses == [JobStatus.QUEUED]
    assert recovered_attempts == [1]
    assert recovered_errors == [None]
    with factory() as session:
        event = session.query(OutboxRecord).filter_by(job_id=running_id).one()
        assert event.queue_name == "default"


def test_startup_recovery_restores_receipts_before_redispatching_running_jobs() -> None:
    factory = _session_factory()
    with factory() as session:
        jobs = JobRepository(session)
        running = jobs.create("transcribe", payload=str(uuid4()))
        jobs.transition(running.id, JobEvent.START)
        running_id = running.id
        session.commit()

    queue = InMemoryJobQueue()
    queue.enqueue("default", "interrupted-job")
    assert queue.claim("default") is not None

    recovered = recover_startup_state(
        session_factory=factory,
        queue=queue,
        queue_names=("default",),
    )

    assert recovered == 1
    assert queue.processing_depth("default") == 0
    assert queue.depth("default") == 1
    with factory() as session:
        event = session.query(OutboxRecord).filter_by(job_id=running_id).one()
        assert event.queue_name == "default"


def test_duplicate_delivery_runs_handler_once_after_queued_job_is_claimed() -> None:
    factory = _session_factory()
    with factory() as session:
        job = JobRepository(session).create("transcribe", payload=str(uuid4()))
        job_id = job.id
        session.commit()

    calls: list[str] = []

    def handle(session: Session, job: object) -> None:
        del session
        calls.append(str(job))

    _process_payload(job_id, factory, {"transcribe": handle})
    _process_payload(job_id, factory, {"transcribe": handle})

    assert len(calls) == 1
    with factory() as session:
        refreshed = JobRepository(session).get(job_id)
        assert refreshed is not None
        assert refreshed.status is JobStatus.SUCCEEDED


@pytest.mark.integration
def test_postgres_lock_allows_only_one_concurrent_duplicate_handler() -> None:
    database_url = os.getenv("DATABASE_URL")
    if database_url is None:
        pytest.skip("DATABASE_URL is not configured")
    parsed = make_url(database_url)
    if not parsed.drivername.startswith("postgresql") or not (
        parsed.database and parsed.database.startswith("openclips_test_")
    ):
        raise pytest.UsageError(
            "DATABASE_URL must target a disposable PostgreSQL database named openclips_test_*"
        )

    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        with factory() as session:
            job = JobRepository(session).create("transcribe", payload=str(uuid4()))
            job_id = job.id
            session.commit()

        handler_entered = threading.Event()
        duplicate_handler_entered = threading.Event()
        release_handler = threading.Event()
        calls: list[str] = []

        def handle(session: Session, job: object) -> None:
            del session
            calls.append(str(job))
            handler_entered.set()
            if len(calls) == 2:
                duplicate_handler_entered.set()
            assert release_handler.wait(timeout=5)

        first = threading.Thread(
            target=_process_payload,
            args=(job_id, factory, {"transcribe": handle}),
        )
        second = threading.Thread(
            target=_process_payload,
            args=(job_id, factory, {"transcribe": handle}),
        )
        first.start()
        assert handler_entered.wait(timeout=5)
        second.start()
        assert not duplicate_handler_entered.wait(timeout=1)
        release_handler.set()
        first.join(timeout=5)
        second.join(timeout=5)

        assert not first.is_alive()
        assert not second.is_alive()
        assert len(calls) == 1
        with factory() as session:
            refreshed = JobRepository(session).get(job_id)
            assert refreshed is not None
            assert refreshed.status is JobStatus.SUCCEEDED
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()

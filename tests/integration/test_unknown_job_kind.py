"""PostgreSQL regression coverage for unknown claimed job kinds."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openclips.domain.jobs import JobStatus
from openclips.domain.outbox import OutboxStatus
from openclips.infrastructure.repositories import JobRepository
from openclips.worker import _process_payload

pytestmark = pytest.mark.integration


def test_unknown_job_kind_fails_without_locking_itself(
    session_factory: sessionmaker[Session],
) -> None:
    fixture_engine = session_factory.kw["bind"]
    engine = create_engine(
        fixture_engine.url,
        connect_args={"options": "-c lock_timeout=3000"},
    )
    factory = sessionmaker(bind=engine)
    try:
        with factory() as session:
            jobs = JobRepository(session)
            job, event = jobs.create_dispatched(
                "legacy_unregistered_kind",
                payload=str(uuid4()),
                queue_name="default",
            )
            event.status = OutboxStatus.DELIVERED
            job_id = job.id
            session.commit()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_process_payload, job_id, factory, {})
            future.result(timeout=30)

        with factory() as session:
            refreshed = JobRepository(session).get(job_id)
            assert refreshed is not None
            assert refreshed.status is JobStatus.FAILED
            assert refreshed.error == "UnknownJobKindError: legacy_unregistered_kind"

        _process_payload(job_id, factory, {})
        with factory() as session:
            duplicate = JobRepository(session).get(job_id)
            assert duplicate is not None
            assert duplicate.status is JobStatus.FAILED
            assert duplicate.attempts == 1
    finally:
        engine.dispose()

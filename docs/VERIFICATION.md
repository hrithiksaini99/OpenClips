# Verification record

## Phase 0 — 2026-08-26

- Local suite: `14 passed, 1 warning`.
- PostgreSQL repository integration: passed in the Compose database.
- Ruff: passed.
- mypy: passed with no issues in 13 source files.
- Docker Compose: API, worker, PostgreSQL, and Redis rebuilt and running.
- Alembic: `upgrade head` passed and `alembic check` reported no new upgrade operations.
- API smoke checks: `/health` returned `{"status":"ok"}` and `/ready` returned healthy database/Redis checks.

The warning is the upstream Starlette deprecation warning emitted by the current test client dependency; it does not fail the suite.

## Phase 1 — 2026-08-26

- Local suite: 68 passed, 12 skipped (PostgreSQL-gated integration tests).
- Full suite in Compose (API container, PostgreSQL + ffmpeg present): 84 passed.
- Adapter coverage: local-upload extension/sanitization/duplicate/failure tests; YouTube canonicalization, shell-free argv construction, progress parsing, stderr-tail failure tests.
- End-to-end ingestion flow: FFmpeg-generated MP4 ingested, replayed bytes reuse one record and one file, rejected uploads create no records, failed sources recover to READY deterministically.
- FFprobe assertions confirmed the stored media is a valid MP4 container.
- Ruff: passed. mypy: no issues in 19 source files.
- Migrations: `downgrade base` → `upgrade head` cycled cleanly; `alembic check` reported no new operations.
- Coordinator fix during verification: re-registering identical bytes for an incomplete source now resumes ingestion instead of returning the stale record (`IngestionCoordinator._recover`).

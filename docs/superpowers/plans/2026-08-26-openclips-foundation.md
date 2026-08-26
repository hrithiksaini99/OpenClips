# OpenClips Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish a tested FastAPI/PostgreSQL/Redis foundation for resumable OpenClips workflows.

**Architecture:** Use a modular monolith with domain state machines isolated from HTTP and infrastructure. Keep persistence, queue, and media paths behind explicit interfaces so later ingestion and AI providers can be added without changing lifecycle rules.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, SQLAlchemy, Alembic, PostgreSQL, Redis, pytest, Ruff, mypy, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-08-26-openclips-foundation-design.md`

## Global Constraints

- V1 is self-hosted and local-AI-first.
- V1 is a single workspace/single organization.
- GPU is optional; CPU-only operation remains supported.
- Source videos default to 7-day retention; generated clips default to forever.
- Do not choose a project license yet.
- No provider SDK or media download integration belongs in this phase.
- Every production behavior requires a failing test before implementation.

---

### Task 1: Repository and runtime scaffold

**Files:**
- Create: `pyproject.toml`, `.env.example`, `.gitignore`, `README.md`
- Create: `src/openclips/__init__.py`, `src/openclips/main.py`, `tests/test_health.py`
- Create: `Dockerfile`, `docker-compose.yml`, `.github/workflows/ci.yml`

**Interfaces:**
- Produces `openclips.main:create_app()` and `/health` plus `/ready` endpoints.

- [ ] Write tests for health response shape and readiness response shape.
- [ ] Run the focused tests and confirm they fail because the app is absent.
- [ ] Implement the minimal typed FastAPI app and configuration loader.
- [ ] Run focused tests, then lint and type checks.
- [ ] Verify Compose configuration parses and services have health checks.

### Task 2: Domain lifecycle model

**Files:**
- Create: `src/openclips/domain/errors.py`, `src/openclips/domain/jobs.py`, `src/openclips/domain/clips.py`
- Create: `tests/domain/test_jobs.py`, `tests/domain/test_clips.py`

**Interfaces:**
- `JobStatus`, `JobEvent`, `JobStateMachine.transition(status, event)`.
- `ClipStatus`, `ClipEvent`, `ClipStateMachine.transition(status, event)`.

- [ ] Write one test for each legal transition and one test for representative illegal transitions.
- [ ] Run domain tests and confirm the missing enums/state machines fail correctly.
- [ ] Implement immutable enums and transition tables with a domain-specific error.
- [ ] Run all domain tests and check error messages are deterministic.
- [ ] Refactor only after tests are green.

### Task 3: Persistence boundary and migrations

**Files:**
- Create: `src/openclips/infrastructure/db.py`, `src/openclips/infrastructure/models.py`, `src/openclips/infrastructure/repositories.py`
- Create: `alembic.ini`, `alembic/env.py`, `alembic/versions/0001_foundation.py`
- Create: `tests/integration/test_persistence.py`

**Interfaces:**
- `JobRepository.create`, `get`, and `transition` persist job status and attempts.
- `ClipRepository.create`, `get`, and `transition` persist clip status.

- [ ] Write repository integration tests against PostgreSQL for create and transition.
- [ ] Run them against the Compose database and confirm failure before implementation.
- [ ] Implement SQLAlchemy models, session factory, repositories, and first migration.
- [ ] Run integration tests and migration upgrade/downgrade checks.
- [ ] Ensure invalid transitions never reach the database as a mutation.

### Task 4: Worker shell and operational checks

**Files:**
- Create: `src/openclips/worker.py`, `src/openclips/application/health.py`
- Modify: `docker-compose.yml`, `README.md`, `docs/PHASES.md`
- Create: `tests/test_worker.py`

**Interfaces:**
- `openclips.worker:run()` starts a bounded worker shell without importing provider implementations.
- Readiness checks report database and Redis connectivity independently.

- [ ] Write tests for bounded worker configuration and dependency readiness behavior.
- [ ] Run focused tests and confirm missing worker/readiness code fails.
- [ ] Implement the worker entry point and dependency probes.
- [ ] Run the complete suite, static checks, and Compose smoke test.
- [ ] Update Phase 0 status only after the main validator inspects the final diff and records evidence.

## Phase gate

Run from the repository root:

```bash
docker compose up -d db redis api worker
docker compose exec api alembic upgrade head
docker compose exec api pytest -q
docker compose exec api ruff check .
docker compose exec api mypy src
curl --fail http://localhost:8000/health
curl --fail http://localhost:8000/ready
```

Phase 1 cannot begin until every command succeeds and the domain, persistence, and operational requirements are manually checked against `docs/PHASES.md`.

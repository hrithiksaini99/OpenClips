# OpenClips Media Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe, idempotent local-upload and YouTube-video ingestion with durable source metadata and recovery behavior.

**Architecture:** Establish source lifecycle and repository contracts first. Build local-upload and YouTube adapters independently against those contracts, then integrate them through one application service and verification gate.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy/Alembic, PostgreSQL, filesystem storage, yt-dlp, FFmpeg/FFprobe, pytest, Ruff, mypy, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-08-26-media-ingestion-design.md`

## Global Constraints

- Accept only local `.mp4` and `.mov` uploads and canonical HTTP(S) YouTube watch URLs in this phase.
- Source retention defaults to seven days.
- Generated-clip retention is unchanged.
- File paths must remain beneath `OPENCLIPS_MEDIA_ROOT`.
- Duplicate requests reuse an existing source by stable SHA-256 idempotency key.
- yt-dlp must run without a shell and external process failures must preserve stderr safely.
- No YouTube channel polling, transcription, clip selection, rendering, UI, or social publishing in this phase.
- Use only OpenCode model `opencode/x-preview-f-free` with variant `max` for coding sessions.

---

### Task 1: Source domain, persistence, and storage contract

**Files:**
- Create: `src/openclips/domain/sources.py`
- Create: `src/openclips/application/ingestion.py`
- Create: `src/openclips/infrastructure/media_storage.py`
- Modify: `src/openclips/infrastructure/models.py`
- Modify: `src/openclips/infrastructure/repositories.py`
- Create: `alembic/versions/0002_source_assets.py`
- Test: `tests/domain/test_sources.py`
- Test: `tests/test_media_storage.py`
- Test: `tests/integration/test_source_repository.py`

**Interfaces:**
- Produces `SourceKind`, `SourceStatus`, `SourceEvent`, `SourceStateMachine.transition`.
- Produces `StoredMedia`, `MediaStorage.write_stream(key, chunks)`, and `SourceRepository` create/get/idempotency/transition operations.
- Produces `IngestionCoordinator.register(...)` and `retry(source_id)`.

- [ ] Write failing tests proving legal/rejected source transitions, path traversal rejection, atomic cleanup, and duplicate idempotency reuse.
- [ ] Run focused tests and confirm failures are caused by missing Phase 1 behavior.
- [ ] Implement the minimal source domain, atomic media storage, model/repository, migration, and coordinator.
- [ ] Run focused unit and PostgreSQL integration tests until green.
- [ ] Run Ruff and mypy for all touched modules and commit the task.

### Task 2: Independent provider adapters

**Files — local-upload workstream:**
- Create: `src/openclips/providers/local_upload.py`
- Create: `tests/providers/test_local_upload.py`

**Files — YouTube workstream:**
- Create: `src/openclips/providers/youtube.py`
- Create: `tests/providers/test_youtube.py`
- Modify: `pyproject.toml`
- Modify: `Dockerfile`

**Interfaces:**
- Local produces `LocalUploadIngestor.ingest(filename, chunks)` using `IngestionCoordinator` and `MediaStorage`.
- YouTube produces `canonicalize_youtube_url(url)`, `extract_youtube_video_id(url)`, and `YtDlpDownloader.download(url, destination, progress)`.

- [ ] In separate worktrees, write failing tests for each adapter before implementation.
- [ ] Local tests must cover extension case handling, invalid extensions, stream hashing, duplicate content, and safe filenames.
- [ ] YouTube tests must cover accepted/rejected hosts, canonical IDs, argv construction without a shell, progress parsing, and process failure.
- [ ] Implement each adapter inside its declared file ownership and commit each workstream separately.
- [ ] Main validator integrates both commits and runs both focused suites together.

### Task 3: Integrated ingestion recovery and media gate

**Files:**
- Modify: `src/openclips/application/ingestion.py`
- Modify: `README.md`
- Modify: `docs/PHASES.md`
- Modify: `docs/VERIFICATION.md`
- Create: `tests/integration/test_ingestion_flow.py`
- Create: `tests/fixtures/README.md`

**Interfaces:**
- Consumes both adapters and the source repository contract.
- Produces a tested local-upload end-to-end flow and retry semantics suitable for the Phase 2 transcription consumer.

- [ ] Write a failing integration test that ingests a generated tiny MP4, replays the same request, and asserts one source record and one final file.
- [ ] Implement only the orchestration needed for the integration flow and deterministic retry behavior.
- [ ] Generate a tiny fixture with FFmpeg during the test or gate; verify it with FFprobe rather than storing a binary fixture in Git.
- [ ] Run migrations up/down/up, all tests with PostgreSQL, Ruff, mypy, Compose health checks, and FFprobe assertions.
- [ ] Record exact evidence and mark Phase 1 verified only after the main validator reviews the integrated diff.

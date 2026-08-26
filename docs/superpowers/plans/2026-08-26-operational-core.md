# OpenClips Operational Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make one authenticated local upload or YouTube URL run durably from ingestion through local transcription, clip selection, and 9:16 rendering in the documented Docker Compose stack.

**Architecture:** PostgreSQL owns jobs and transactional outbox events; the worker relays due events to reliable Redis ready/processing lists and executes only atomically claimed `QUEUED` jobs. API and worker share persistent media, the worker auto-downloads faster-whisper into a persistent model cache, and successful automatic stages create their successor jobs in the same transaction.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLAlchemy, Alembic, PostgreSQL 16, Redis 7, faster-whisper, yt-dlp, FFmpeg/FFprobe, Docker Compose, pytest, Ruff, and strict mypy.

**Spec:** `docs/superpowers/specs/2026-08-26-operational-core-design.md`

## Global Constraints

- Preserve the modular-monolith domain/application/infrastructure/provider boundaries.
- PostgreSQL is authoritative; Redis is at-least-once delivery only.
- All external commands use argv lists with no shell, bounded timeouts, and truncated errors.
- API and worker use `/data/media`; faster-whisper uses `/root/.cache/huggingface`.
- Automatic processing defaults to enabled; existing manual stage endpoints remain supported.
- Model download occurs automatically on first transcription and persists across restarts.
- V1 recovery targets one Compose worker service; multi-host leases are out of scope.
- Tests must never destroy the developer database or named media/model volumes.
- Every production change follows a red-green test cycle and strict typing.

---

## File and responsibility map

- `src/openclips/domain/outbox.py`: outbox status and deterministic relay backoff.
- `src/openclips/infrastructure/models.py`: `OutboxRecord` and source automation persistence.
- `src/openclips/infrastructure/repositories.py`: atomic job/outbox creation, outbox claims, retries, and recovery.
- `src/openclips/infrastructure/queue.py`: reliable ready/processing queue receipts and acknowledgements.
- `src/openclips/application/dispatch.py`: outbox-to-Redis relay orchestration.
- `src/openclips/application/youtube_ingestion.py`: durable YouTube registration and worker execution.
- `src/openclips/application/pipeline.py`: queue-name mapping and successor-job helpers.
- `src/openclips/api/routes.py`: upload, YouTube, retry, and readiness HTTP endpoints.
- `src/openclips/api/schemas.py`: operational-core request/response contracts.
- `src/openclips/providers/faster_whisper_provider.py`: model-cache marker and readiness state.
- `src/openclips/worker.py`: relay/consumer loop, reliable acknowledgement, handlers, and startup recovery.
- `alembic/versions/0008_operational_core.py`: source automation and outbox schema migration.
- `tests/integration/test_operational_pipeline.py`: real PostgreSQL/Redis/FFmpeg automatic-flow test.
- `scripts/verify.sh`: isolated full verification gate.

---

### Task 1: Deployable shared runtime

**Files:**
- Create: `.dockerignore`
- Modify: `pyproject.toml`
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `src/openclips/config.py`
- Create: `tests/test_deployment.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.max_upload_bytes: int`, `Settings.outbox_batch_size: int`, `Settings.outbox_backoff_cap_seconds: int`, `Settings.model_cache_root: Path`.
- Produces: named volumes `media-data` and `model-cache` mounted at the paths in Global Constraints.

- [ ] **Step 1: Write failing deployment/config tests**

```python
def test_operational_limits_are_typed() -> None:
    settings = Settings(
        _env_file=None,
        max_upload_bytes=1024,
        outbox_batch_size=7,
        outbox_backoff_cap_seconds=90,
    )
    assert settings.max_upload_bytes == 1024
    assert settings.outbox_batch_size == 7
    assert settings.outbox_backoff_cap_seconds == 90


def test_compose_shares_media_and_model_cache() -> None:
    compose = Path("docker-compose.yml").read_text()
    assert "media-data:/data/media" in compose
    assert "model-cache:/root/.cache/huggingface" in compose
    assert compose.count("env_file:") == 2
```

- [ ] **Step 2: Run the focused tests and confirm the expected failure**

Run: `pytest tests/test_config.py tests/test_deployment.py -q`

Expected: FAIL because the settings fields and Compose mounts do not exist.

- [ ] **Step 3: Add runtime settings and dependencies**

Add these fields to `Settings`:

```python
max_upload_bytes: int = Field(default=10 * 1024 * 1024 * 1024, ge=1)
outbox_batch_size: int = Field(default=50, ge=1, le=1000)
outbox_backoff_cap_seconds: int = Field(default=300, ge=1)
model_cache_root: Path = Path("/root/.cache/huggingface/hub")
```

Add `python-multipart>=0.0.9` to project dependencies. Install `.[dev,transcription]` in the Docker image.

- [ ] **Step 4: Add shared volumes and environment propagation**

Configure both application services with `.env`, keep their internal database/Redis URL overrides, mount `media-data` into API and worker, mount `model-cache` read-only into API and read-write into worker, and declare both named volumes.

Create `.dockerignore` with:

```text
.git
.env
.venv
.worktrees
.superpowers
__pycache__
.pytest_cache
.ruff_cache
.mypy_cache
data
*.pyc
```

- [ ] **Step 5: Run focused and static checks**

Run: `pytest tests/test_config.py tests/test_deployment.py -q`

Expected: PASS.

Run: `ruff check src/openclips/config.py tests/test_config.py tests/test_deployment.py`

Expected: PASS.

- [ ] **Step 6: Commit the deployable runtime**

```bash
git add .dockerignore .env.example Dockerfile docker-compose.yml pyproject.toml src/openclips/config.py tests/test_config.py tests/test_deployment.py
git commit -m "fix: share operational runtime state"
```

---

### Task 2: Transactional outbox persistence

**Files:**
- Create: `src/openclips/domain/outbox.py`
- Modify: `src/openclips/infrastructure/models.py`
- Modify: `src/openclips/infrastructure/repositories.py`
- Create: `alembic/versions/0008_operational_core.py`
- Create: `tests/domain/test_outbox.py`
- Create: `tests/integration/test_outbox_repository.py`

**Interfaces:**
- Produces: `OutboxStatus`, `outbox_backoff_seconds(attempts: int, cap_seconds: int) -> int`.
- Produces: `JobRepository.create_dispatched(kind: str, *, payload: str | None, queue_name: str) -> tuple[JobRecord, OutboxRecord]`.
- Produces: `OutboxRepository.due(now: datetime, limit: int) -> list[OutboxRecord]`, `mark_delivered(event_id: UUID, delivered_at: datetime)`, and `mark_failed(event_id: UUID, error: str, next_attempt_at: datetime)`.
- Produces: `SourceAssetRecord.auto_process: bool` with a database default of true.

- [ ] **Step 1: Write failing domain and repository tests**

```python
def test_outbox_backoff_is_exponential_and_capped() -> None:
    assert outbox_backoff_seconds(1, 300) == 1
    assert outbox_backoff_seconds(2, 300) == 2
    assert outbox_backoff_seconds(20, 300) == 300


def test_create_dispatched_flushes_job_and_pending_outbox(session: Session) -> None:
    job, event = JobRepository(session).create_dispatched(
        "transcribe", payload=str(uuid4()), queue_name="default"
    )
    assert event.job_id == job.id
    assert event.queue_name == "default"
    assert event.status is OutboxStatus.PENDING
```

- [ ] **Step 2: Run tests and verify red state**

Run: `pytest tests/domain/test_outbox.py tests/integration/test_outbox_repository.py -q`

Expected: FAIL because the outbox module, model, migration, and repository do not exist.

- [ ] **Step 3: Implement domain and ORM records**

```python
class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"


def outbox_backoff_seconds(attempts: int, cap_seconds: int) -> int:
    if attempts < 1:
        raise ValueError("Outbox backoff requires at least one attempt")
    return min(2 ** (attempts - 1), cap_seconds)
```

Add `OutboxRecord` with UUID ID, indexed job foreign key, queue name, status, attempts, available-at, last error, delivered-at, and timestamps. Add `auto_process` to `SourceAssetRecord`.

- [ ] **Step 4: Implement repositories and migration**

`0008_operational_core` adds `source_assets.auto_process` and creates `outbox_events` plus due-event indexes. `JobRepository.create_dispatched` adds and flushes both records without committing. `OutboxRepository.due` filters pending due rows and uses `with_for_update(skip_locked=True)` on PostgreSQL.

- [ ] **Step 5: Run migration and repository gates**

Run: `pytest tests/domain/test_outbox.py tests/integration/test_outbox_repository.py -q`

Expected: PASS when `DATABASE_URL` is configured; the integration test otherwise skips explicitly.

Run: `docker compose exec -T api alembic upgrade head && docker compose exec -T api alembic check`

Expected: `No new upgrade operations detected.`

- [ ] **Step 6: Commit outbox persistence**

```bash
git add src/openclips/domain/outbox.py src/openclips/infrastructure/models.py src/openclips/infrastructure/repositories.py alembic/versions/0008_operational_core.py tests/domain/test_outbox.py tests/integration/test_outbox_repository.py
git commit -m "feat: persist transactional job outbox"
```

---

### Task 3: Reliable Redis claims and outbox relay

**Files:**
- Modify: `src/openclips/infrastructure/queue.py`
- Create: `src/openclips/application/dispatch.py`
- Modify: `src/openclips/worker.py`
- Modify: `tests/test_queue.py`
- Create: `tests/test_dispatch.py`

**Interfaces:**
- Produces: `QueueReceipt(queue_name: str, payload: str)`.
- Produces: queue methods `claim(...) -> QueueReceipt | None`, `ack(receipt: QueueReceipt) -> None`, and `restore_processing(queue_name: str) -> int`.
- Produces: `OutboxRelay.dispatch_once() -> int`.

- [ ] **Step 1: Write failing reliable-claim tests**

```python
def test_claim_moves_message_until_ack() -> None:
    queue = InMemoryJobQueue()
    queue.enqueue("default", "job-1")
    receipt = queue.claim("default", timeout_seconds=0)
    assert receipt == QueueReceipt("default", "job-1")
    assert queue.depth("default") == 0
    assert queue.processing_depth("default") == 1
    queue.ack(receipt)
    assert queue.processing_depth("default") == 0


def test_restore_processing_redelivers_unacked_message() -> None:
    queue = InMemoryJobQueue()
    queue.enqueue("default", "job-1")
    queue.claim("default", timeout_seconds=0)
    assert queue.restore_processing("default") == 1
    assert queue.claim("default", timeout_seconds=0).payload == "job-1"
```

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `pytest tests/test_queue.py tests/test_dispatch.py -q`

Expected: FAIL because receipts, processing lists, acknowledgements, and relay do not exist.

- [ ] **Step 3: Implement reliable in-memory and Redis queues**

Use one ready deque/list and one processing deque/list per queue. Redis claim uses atomic `BLMOVE ... LEFT RIGHT` from `openclips:{queue}:ready` to `openclips:{queue}:processing`. Acknowledgement uses `LREM`; startup restore moves every processing payload back to ready.

```python
@dataclass(frozen=True)
class QueueReceipt:
    queue_name: str
    payload: str
```

- [ ] **Step 4: Implement the outbox relay**

`OutboxRelay` receives a session factory, queue, clock, batch size, and backoff cap. It locks one due batch, enqueues each job ID, marks successes delivered, records individual Redis failures with their next-attempt time, and commits once per batch.

- [ ] **Step 5: Update worker acknowledgement semantics**

`process_once` claims a `QueueReceipt`, runs `_process_payload`, and acknowledges only after `_process_payload` has committed success, failure, duplicate-ignore, or unknown-job handling. Exceptions that prevent a durable database outcome leave the receipt in processing for startup recovery.

- [ ] **Step 6: Run focused tests and typing**

Run: `pytest tests/test_queue.py tests/test_dispatch.py -q`

Expected: PASS.

Run: `mypy src`

Expected: PASS.

- [ ] **Step 7: Commit reliable delivery**

```bash
git add src/openclips/infrastructure/queue.py src/openclips/application/dispatch.py src/openclips/worker.py tests/test_queue.py tests/test_dispatch.py
git commit -m "feat: relay jobs with reliable redis claims"
```

---

### Task 4: Durable manual dispatch, retry, and startup recovery

**Files:**
- Create: `src/openclips/application/pipeline.py`
- Modify: `src/openclips/domain/jobs.py`
- Modify: `src/openclips/infrastructure/repositories.py`
- Modify: `src/openclips/application/transcription.py`
- Modify: `src/openclips/application/clipping.py`
- Modify: `src/openclips/application/rendering.py`
- Modify: `src/openclips/api/routes.py`
- Modify: `src/openclips/worker.py`
- Modify: `tests/test_api.py`
- Modify: `tests/test_publishing.py`
- Create: `tests/test_job_recovery.py`

**Interfaces:**
- Produces: `queue_for_job_kind(kind: str) -> str` returning platform job kinds unchanged and `default` for pipeline jobs.
- Produces: `JobEvent.RECOVER`, allowing `RUNNING -> QUEUED` without incrementing attempts.
- Produces: `JobRepository.retry_dispatched(job_id: UUID) -> tuple[JobRecord, OutboxRecord]` and `recover_running() -> list[JobRecord]`.
- Produces: authenticated `POST /api/v1/jobs/{job_id}/retry`.

- [ ] **Step 1: Write failing API and recovery tests**

```python
def test_manual_transcribe_creates_pending_outbox(client) -> None:
    test_client, factory = client
    ids = _seed(factory)
    response = test_client.post(
        f"/api/v1/sources/{ids['source_id']}/transcribe",
        headers=_auth(),
    )
    assert response.status_code == 200
    with factory() as session:
        assert session.query(OutboxRecord).filter_by(job_id=UUID(response.json()["job_id"])).one()


def test_retry_failed_job_creates_new_dispatch(client) -> None:
    test_client, factory = client
    with factory() as session:
        jobs = JobRepository(session)
        failed = jobs.create("transcribe", payload=str(uuid4()))
        jobs.transition(failed.id, JobEvent.START)
        jobs.transition(failed.id, JobEvent.FAIL, error="forced test failure")
        session.commit()
    response = test_client.post(f"/api/v1/jobs/{failed.id}/retry", headers=_auth())
    assert response.status_code == 200
    assert response.json()["status"] == "QUEUED"
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `pytest tests/test_api.py tests/test_job_recovery.py -q`

Expected: FAIL because manual endpoints create database-only jobs and retry/recovery APIs do not exist.

- [ ] **Step 3: Route every coordinator enqueue through the outbox**

Replace `jobs.create(...)` in transcription, selection, rendering, and publication due-dispatch with:

```python
job, _event = self.jobs.create_dispatched(
    JOB_KIND,
    payload=str(entity_id),
    queue_name=queue_for_job_kind(JOB_KIND),
)
return job
```

- [ ] **Step 4: Implement retry and startup recovery**

Add the `RECOVER` transition, clear stale errors on `RETRY`/`RECOVER`, create a fresh outbox event for both paths, restore Redis processing lists before recovering database jobs, and perform recovery once before the worker loop starts.

- [ ] **Step 5: Ignore duplicate deliveries atomically**

Before `START`, lock the job row. If status is not `QUEUED`, commit no state change and return a durable ignored result so the receipt can be acknowledged. Add concurrent/duplicate tests using two sessions against PostgreSQL.

- [ ] **Step 6: Run focused tests**

Run: `pytest tests/test_api.py tests/test_job_recovery.py tests/test_transcription_coordinator.py tests/test_clip_selection_coordinator.py tests/test_render_coordinator.py tests/test_publishing.py -q`

Expected: PASS.

- [ ] **Step 7: Commit durable dispatch and recovery**

```bash
git add src/openclips/application/pipeline.py src/openclips/domain/jobs.py src/openclips/infrastructure/repositories.py src/openclips/application/transcription.py src/openclips/application/clipping.py src/openclips/application/rendering.py src/openclips/api/routes.py src/openclips/worker.py tests/test_api.py tests/test_publishing.py tests/test_job_recovery.py
git commit -m "fix: dispatch and recover durable jobs"
```

---

### Task 5: Authenticated upload and background YouTube ingestion

**Files:**
- Modify: `src/openclips/infrastructure/media_storage.py`
- Modify: `src/openclips/application/ingestion.py`
- Create: `src/openclips/application/youtube_ingestion.py`
- Modify: `src/openclips/providers/youtube.py`
- Modify: `src/openclips/application/services.py`
- Modify: `src/openclips/api/schemas.py`
- Modify: `src/openclips/api/routes.py`
- Modify: `src/openclips/worker.py`
- Create: `tests/test_ingestion_api.py`
- Create: `tests/test_youtube_ingestion.py`

**Interfaces:**
- Produces: `MediaStorage.promote_file(key: str, temporary_path: Path) -> StoredMedia`.
- Produces: `YouTubeIngestionCoordinator.register(url: str, auto_process: bool) -> tuple[SourceAssetRecord, JobRecord]` and `run(job: JobRecord) -> SourceAssetRecord`.
- Produces: `SourceIngestOut(source: SourceOut, next_job: EnqueueJobOut | None)`.

- [ ] **Step 1: Write failing ingestion API tests**

```python
def test_upload_streams_source_and_enqueues_transcription(client, tiny_mp4) -> None:
    with tiny_mp4.open("rb") as media:
        response = client.post(
            "/api/v1/sources/upload",
            files={"file": ("episode.mp4", media, "video/mp4")},
            data={"auto_process": "true"},
            headers=_auth(),
        )
    assert response.status_code == 202
    assert response.json()["source"]["status"] == "READY"
    assert response.json()["next_job"]["kind"] == "transcribe"


def test_youtube_returns_before_background_download(client) -> None:
    response = client.post(
        "/api/v1/sources/youtube",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "auto_process": True},
        headers=_auth(),
    )
    assert response.status_code == 202
    assert response.json()["source"]["status"] == "PENDING"
    assert response.json()["next_job"]["kind"] == "ingest_youtube"
```

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `pytest tests/test_ingestion_api.py tests/test_youtube_ingestion.py -q`

Expected: FAIL with missing routes/coordinator.

- [ ] **Step 3: Add bounded upload streaming**

Read `UploadFile.file` in 64 KiB chunks while counting bytes; raise HTTP 413 before yielding a chunk that crosses `Settings.max_upload_bytes`. Pass chunks to `LocalUploadIngestor`, persist `auto_process`, and create the transcription job/outbox only when automation is enabled.

- [ ] **Step 4: Implement YouTube registration and execution**

Canonicalize the URL, compute `sha256(f"youtube:{video_id}".encode()).hexdigest()`, reuse an existing source by that key, and create `ingest_youtube` through `create_dispatched`. The worker downloads to a uniquely named `.partial` file, calls `promote_file`, attaches media metadata, removes the partial file in `finally`, and preserves downloader errors.

- [ ] **Step 5: Register the new worker handler and API schemas**

Add `INGEST_YOUTUBE_JOB_KIND = "ingest_youtube"`, inject `YtDlpDownloader` and storage through services/handler construction, and expose both authenticated endpoints with `202` responses.

- [ ] **Step 6: Run ingestion gates**

Run: `pytest tests/test_ingestion_api.py tests/test_youtube_ingestion.py tests/providers/test_local_upload.py tests/providers/test_youtube.py tests/integration/test_ingestion_flow.py -q`

Expected: PASS, with PostgreSQL tests skipping only when `DATABASE_URL` is absent.

- [ ] **Step 7: Commit ingestion entry points**

```bash
git add src/openclips/infrastructure/media_storage.py src/openclips/application/ingestion.py src/openclips/application/youtube_ingestion.py src/openclips/providers/youtube.py src/openclips/application/services.py src/openclips/api/schemas.py src/openclips/api/routes.py src/openclips/worker.py tests/test_ingestion_api.py tests/test_youtube_ingestion.py
git commit -m "feat: ingest uploads and youtube in background"
```

---

### Task 6: Automatic stage chaining

**Files:**
- Modify: `src/openclips/application/youtube_ingestion.py`
- Modify: `src/openclips/application/transcription.py`
- Modify: `src/openclips/application/clipping.py`
- Modify: `src/openclips/worker.py`
- Create: `tests/test_pipeline_chaining.py`
- Modify: `tests/test_transcription_coordinator.py`
- Modify: `tests/test_clip_selection_coordinator.py`

**Interfaces:**
- Consumes: `SourceAssetRecord.auto_process`, `JobRepository.create_dispatched`, and `queue_for_job_kind`.
- Produces: successful automatic stages create their successor jobs before the worker transaction commits.

- [ ] **Step 1: Write failing chain tests**

```python
def test_transcription_success_dispatches_selection(
    tmp_path: Path, harness: _Harness
) -> None:
    source = _ready_source(harness, tmp_path)
    source.auto_process = True
    _provider(harness).outcomes.append(_document())
    transcription_job = harness.transcription.enqueue(source.id)

    harness.transcription.run(transcription_job)

    selection = harness.transcription.jobs.list_all(kind="select_clips")
    assert len(selection) == 1
    event = harness.transcription.jobs.outbox_for_job(selection[0].id)
    assert event is not None and event.status is OutboxStatus.PENDING


def test_selection_dispatches_one_render_per_candidate(harness: _Harness) -> None:
    source = _ready_source(harness)
    source.auto_process = True
    harness.transcripts.upsert_for_source(source.id, _document())
    coordinator = _coordinator(harness, HeuristicClipRefiner())
    selection_job = coordinator.enqueue(source.id)

    clips = coordinator.run(selection_job)

    render_jobs = harness.jobs.list_all(kind="render_clip")
    assert [job.payload for job in render_jobs] == [str(clip.id) for clip in clips]
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `pytest tests/test_pipeline_chaining.py tests/test_transcription_coordinator.py tests/test_clip_selection_coordinator.py -q`

Expected: FAIL because successful stages do not create successors.

- [ ] **Step 3: Chain YouTube ingestion and transcription**

After media attachment, enqueue `transcribe` only when `source.auto_process` is true. After transcript upsert, enqueue `select_clips` under the same condition.

- [ ] **Step 4: Fan out rendering after selection**

After deterministic selection persists clips, create one `render_clip` job/outbox per clip when automation is enabled. An empty candidate list creates no render jobs and still lets the selection job succeed.

- [ ] **Step 5: Prove manual mode and sibling isolation**

Add tests that `auto_process=false` creates no successor and that a failed render job does not change sibling render jobs or clips.

- [ ] **Step 6: Run pipeline tests**

Run: `pytest tests/test_pipeline_chaining.py tests/test_transcription_coordinator.py tests/test_clip_selection_coordinator.py tests/test_render_coordinator.py -q`

Expected: PASS.

- [ ] **Step 7: Commit automatic chaining**

```bash
git add src/openclips/application/youtube_ingestion.py src/openclips/application/transcription.py src/openclips/application/clipping.py src/openclips/worker.py tests/test_pipeline_chaining.py tests/test_transcription_coordinator.py tests/test_clip_selection_coordinator.py
git commit -m "feat: chain automatic processing stages"
```

---

### Task 7: First-use model download readiness

**Files:**
- Modify: `src/openclips/providers/faster_whisper_provider.py`
- Modify: `src/openclips/api/schemas.py`
- Modify: `src/openclips/api/routes.py`
- Modify: `src/openclips/application/services.py`
- Modify: `tests/providers/test_faster_whisper.py`
- Create: `tests/test_transcription_readiness_api.py`

**Interfaces:**
- Produces: `TranscriptionReadiness(StrEnum)` with `MISSING`, `DOWNLOADING`, and `AVAILABLE`.
- Produces: `FasterWhisperProvider.download_marker: Path`, scoped to the configured model.
- Produces: `FasterWhisperProvider.readiness_state() -> TranscriptionReadiness`.
- Produces: `GET /api/v1/system/transcription-readiness`.

- [ ] **Step 1: Write failing provider and API tests**

```python
def test_readiness_reports_download_marker(tmp_path: Path) -> None:
    provider = FasterWhisperProvider(model_root=tmp_path)
    provider.download_marker.parent.mkdir(parents=True, exist_ok=True)
    provider.download_marker.touch()
    assert provider.readiness_state() is TranscriptionReadiness.DOWNLOADING


def test_readiness_endpoint_is_non_gating(client) -> None:
    response = client.get("/api/v1/system/transcription-readiness")
    assert response.status_code == 200
    assert response.json()["status"] in {"missing", "downloading", "available"}
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `pytest tests/providers/test_faster_whisper.py tests/test_transcription_readiness_api.py -q`

Expected: FAIL because the state enum, marker, and endpoint do not exist.

- [ ] **Step 3: Implement marker lifecycle**

Before `WhisperModel(...)` begins, atomically create a model-specific `.openclips-downloading` marker in the cache root. Remove it in `finally`. Determine readiness in this order: loaded/model directory exists → available; marker exists → downloading; otherwise missing.

- [ ] **Step 4: Expose readiness without changing `/ready` semantics**

Return a typed lowercase response from the new public read endpoint. Continue returning database/Redis-only status from `/ready`.

- [ ] **Step 5: Run provider/API and static gates**

Run: `pytest tests/providers/test_faster_whisper.py tests/test_transcription_readiness_api.py tests/test_health.py -q`

Expected: PASS.

Run: `ruff check src tests && mypy src`

Expected: PASS.

- [ ] **Step 6: Commit model readiness**

```bash
git add src/openclips/providers/faster_whisper_provider.py src/openclips/api/schemas.py src/openclips/api/routes.py src/openclips/application/services.py tests/providers/test_faster_whisper.py tests/test_transcription_readiness_api.py
git commit -m "feat: report first-use transcription readiness"
```

---

### Task 8: Operational E2E gate and documentation

**Files:**
- Create: `tests/integration/test_operational_pipeline.py`
- Create: `tests/integration/test_redis_dispatch.py`
- Create: `scripts/verify.sh`
- Modify: `README.md`
- Modify: `docs/PHASES.md`
- Modify: `docs/VERIFICATION.md`

**Interfaces:**
- Consumes: all operational-core interfaces from Tasks 1–7.
- Produces: executable `scripts/verify.sh` that uses an isolated PostgreSQL database and preserves named developer data.

- [ ] **Step 1: Write the failing real-dependency integration test**

The test must generate a 25-second MP4 with FFmpeg, POST it through the authenticated API, relay and consume jobs using real PostgreSQL and Redis, inject a deterministic `TranscriptionProvider`, run real clip selection and FFmpeg rendering, and assert:

```python
assert source.status is SourceStatus.READY
assert transcript is not None
assert all(clip.status is ClipStatus.READY_FOR_REVIEW for clip in clips)
assert all(storage.resolve(clip.output_path).is_file() for clip in clips)
assert all(clip.render_width == 1080 and clip.render_height == 1920 for clip in clips)
```

- [ ] **Step 2: Run the E2E test and verify failure**

Run: `DATABASE_URL=postgresql+psycopg://openclips:openclips@localhost:5432/openclips_test REDIS_URL=redis://localhost:6379/15 pytest tests/integration/test_operational_pipeline.py tests/integration/test_redis_dispatch.py -q`

Expected: FAIL until every operational path is connected.

- [ ] **Step 3: Add the isolated verification script**

`scripts/verify.sh` must:

```bash
#!/usr/bin/env bash
set -euo pipefail

VERIFY_DB="openclips_verify_$$"
cleanup() {
  docker compose exec -T redis redis-cli -n 15 FLUSHDB >/dev/null || true
  docker compose exec -T db dropdb --force --if-exists -U openclips "$VERIFY_DB" >/dev/null || true
}
trap cleanup EXIT

docker compose up -d db redis
docker compose build api worker
docker compose exec -T redis redis-cli -n 15 FLUSHDB >/dev/null
docker compose exec -T db createdb -U openclips "$VERIFY_DB"
VERIFY_URL="postgresql+psycopg://openclips:openclips@db:5432/$VERIFY_DB"
docker compose run --rm --no-deps \
  -e DATABASE_URL="$VERIFY_URL" \
  -e REDIS_URL="redis://redis:6379/15" \
  api pytest -q
docker compose run --rm --no-deps api ruff check src tests
docker compose run --rm --no-deps api mypy src
docker compose run --rm --no-deps \
  -e OPENCLIPS_DATABASE_URL="$VERIFY_URL" \
  api alembic upgrade head
docker compose run --rm --no-deps \
  -e OPENCLIPS_DATABASE_URL="$VERIFY_URL" \
  api alembic check
docker compose up -d --force-recreate api
curl --fail --silent http://localhost:8000/health >/dev/null
curl --fail --silent http://localhost:8000/ready >/dev/null
```

Use a Redis database reserved for verification and clear only that database before/after the integration test.

- [ ] **Step 4: Run the complete verification gate**

Run: `chmod +x scripts/verify.sh && ./scripts/verify.sh`

Expected: all pytest tests pass with zero failures; Ruff passes; mypy reports no issues; Alembic reports no new operations; health/readiness curls succeed.

- [ ] **Step 5: Update lifecycle and phase truthfulness**

Document exact start, migrate, verify, logs, and stop commands in `README.md`. Update `docs/PHASES.md` and `docs/VERIFICATION.md` with only evidence produced by Step 4. Do not claim live faster-whisper or live YouTube success unless their opt-in smoke tests were actually run.

- [ ] **Step 6: Re-run final-tree verification**

Run: `./scripts/verify.sh`

Expected: the same green result after documentation changes.

Run: `git diff --check && git status --short`

Expected: no whitespace errors; only intended files are modified before commit.

- [ ] **Step 7: Commit the operational core gate**

```bash
git add scripts/verify.sh tests/integration/test_operational_pipeline.py tests/integration/test_redis_dispatch.py README.md docs/PHASES.md docs/VERIFICATION.md
git commit -m "test: verify operational core end to end"
```

---

## Final review gate

- [ ] Confirm every acceptance criterion in the spec has current command evidence.
- [ ] Run `./scripts/verify.sh` on the final commit.
- [ ] Run `git log --oneline origin/main..HEAD` and review every operational-core commit.
- [ ] Run `git diff --check origin/main...HEAD`.
- [ ] Push `fix/operational-core` and create a pull request only after user review.

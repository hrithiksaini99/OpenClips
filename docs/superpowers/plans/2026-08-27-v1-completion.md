# OpenClips V1 Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the gap between the verified operational core and the V1 product in `docs/PRD.md`. After this subproject a self-hosted operator can register a YouTube video, a YouTube channel, or a local upload; watch clips render automatically with real operator-set speaker framing; review, edit, and approve them in a usable dashboard; write Instagram and YouTube copy; schedule each platform independently through an authenticated API; and have approved clips dispatched, published, and retried by the running worker — while three confirmed release-blocking defects in shipped code are fixed first, expired source media is purged, and generated clips are retained forever.

**Architecture:** PostgreSQL stays authoritative and every new dispatch goes through the existing transactional outbox; Redis is at-least-once only. The worker owns a bounded `ThreadPoolExecutor` plus a global `BoundedSemaphore` and a per-stage `StageLimiter`, and its main loop also drives `PublicationScheduler.dispatch_once`, `ChannelPoller.poll_due`, and `RetentionSweeper.sweep` on independent intervals. Publications gain a `QUEUED`/`CANCELLED` lifecycle and an atomic `claim_due` (`SELECT … FOR UPDATE SKIP LOCKED`) so repeated scheduler passes and concurrent workers still produce exactly one publish job per due publication. Per-platform `DailyWindowRule`s and a `latest_scheduled_at` watermark lay approvals across successive slots. Post copy is composed deterministically from the clip's own transcript, stored in a dedicated `clip_platform_copy` table, and validated against platform limits. Instagram's public-URL requirement is solved behind a `PublicMediaUrlProvider`; the renderer gains deterministic `FocusCropStrategy` and `SplitScreenLayout` arithmetic. The dashboard is a server-rendered Jinja2 page plus vanilla JavaScript against the documented JSON API.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLAlchemy, Alembic, PostgreSQL 16, Redis 7, faster-whisper, yt-dlp, FFmpeg/FFprobe, Jinja2, Docker Compose, pytest, Ruff, and strict mypy.

**Spec:** `docs/superpowers/specs/2026-08-27-v1-completion-design.md`

## Global Constraints

- Preserve the modular-monolith domain/application/infrastructure/provider boundaries.
- PostgreSQL is authoritative; Redis is at-least-once delivery only. Every new job dispatch is created through `JobRepository.create_dispatched` in the same transaction as the state change that produced it.
- No worker code path holds a database row lock (`SELECT … FOR UPDATE`) across a second session that touches the same row.
- No media operation mutates the filesystem outside `media_root`, including on the rejection path.
- All external commands use argv lists with no shell, bounded timeouts, and truncated stderr tails.
- Post copy and framing require no paid API and no network: copy is composed from the clip transcript, framing is computed arithmetically from configured focus points.
- Every production change follows a red-green test cycle. Tests assert real behavior (state, filesystem, argv, HTTP), never source text or "a function was called".
- Ruff (`ruff check src tests`) and strict mypy (`mypy src`) pass after every task.
- Integration tests use a disposable PostgreSQL database named `openclips_test_*` and the reserved Redis logical database `15`; they never touch the developer database or named media/model volumes.
- The repository adds no `LICENSE` file and no `license` field in `pyproject.toml`; the open decision in the PRD stays open.
- No credentials are committed. No `git push`, merge, deploy, or service/database mutation. Work happens on branch `fix/v1-completion` with local commits only.
- Each task ends with exactly one commit using the subject given in that task's final step; no extra body or attribution trailer is added.

## Local gate shorthand

- `PG` = `DATABASE_URL=postgresql+psycopg://openclips:openclips@localhost:5432/openclips_test_local` (operator runs `createdb openclips_test_local` once; `conftest.py` requires the `openclips_test_` prefix).
- `REDIS` = `REDIS_URL=redis://localhost:6379/15`.
- The authoritative full gate is `./scripts/verify.sh`, which runs the same suites inside the operational-core image against a disposable `openclips_test_$$` database and Redis db 15.
- Migration round trip: `alembic upgrade head` → `alembic check` (expect `No new upgrade operations detected.`) → `alembic downgrade -1` → `alembic upgrade head`, run through `docker compose run --rm --no-deps -e OPENCLIPS_DATABASE_URL=<disposable> api alembic …`.

## File and responsibility map

- `src/openclips/application/pipeline.py`: canonical `KNOWN_JOB_KINDS` registry and queue routing.
- `src/openclips/infrastructure/media_storage.py`: contained directory materialization (`_prepare_target`) and `delete`.
- `src/openclips/infrastructure/queue.py`: FIFO-preserving `restore_processing`.
- `src/openclips/application/concurrency.py`: `StageLimiter` named semaphores.
- `src/openclips/worker.py`: bounded pool, stage limits, and the scheduler/channel/retention loop drivers.
- `src/openclips/domain/publishing.py`: publication lifecycle enums and state machine.
- `src/openclips/application/publishing.py`: `ScheduleCoordinator` scheduling, atomic `enqueue_due`, cancellation, copy-aware requests, per-platform rules.
- `src/openclips/application/scheduler.py`: `PublicationScheduler` production caller.
- `src/openclips/providers/media_urls.py`: `PublicMediaUrlProvider` abstraction.
- `src/openclips/domain/copy.py` / `src/openclips/application/copy.py`: copy limits and deterministic composer.
- `src/openclips/domain/channels.py` / `src/openclips/application/channels.py`: channel due-computation and polling.
- `src/openclips/domain/retention.py` / `src/openclips/application/retention.py`: purge predicate and sweeper.
- `src/openclips/providers/renderer.py` / `src/openclips/application/rendering.py`: focus-crop and split-screen layouts.
- `src/openclips/api/publishing_routes.py` / `src/openclips/api/channel_routes.py`: new admin-guarded routers.
- `src/openclips/api/templates/` + `src/openclips/api/static/`: dashboard page and assets.
- `alembic/versions/0009_*` … `0013_*`: dispatch index, copy table, channels table, retention column, framing columns.
- `tests/smoke/`: opt-in real-provider suites.
- `progress.md`, `docs/PHASES.md`, `docs/VERIFICATION.md`, `README.md`: truthful status.

## File ownership and parallelization

Tasks 1–3 are the immediate release blockers. They touch disjoint files —
Task 1 (`application/pipeline.py`, `worker.py`, `api/routes.py` retry guard), Task 2
(`infrastructure/media_storage.py`), Task 3 (`infrastructure/queue.py`) — and may be
implemented in parallel. **All three gates must be green before any feature task
starts.**

Every task from 4 onward serializes through at least one of `src/openclips/config.py`,
`src/openclips/worker.py`, `src/openclips/api/routes.py`, `src/openclips/api/schemas.py`,
`src/openclips/application/services.py`, `src/openclips/application/publishing.py`,
`src/openclips/infrastructure/repositories.py`, or `src/openclips/infrastructure/models.py`.
They must be implemented and reviewed **one at a time in the listed order**. Migrations
`0009`→`0013` chain by `down_revision`, so their tasks (6, 9, 11, 12, 13) must land in that
relative order.

---

## Part 1 — Immediate release blockers

### Task 1: PostgreSQL unknown-job-kind deadlock and legacy-job policy

**Files:**
- Modify: `src/openclips/application/pipeline.py`
- Modify: `src/openclips/worker.py`
- Modify: `src/openclips/api/routes.py`
- Create: `tests/test_pipeline.py`
- Create: `tests/integration/test_unknown_job_kind.py`
- Modify: `tests/test_api.py`

**Interfaces:**
- Produces: `KNOWN_JOB_KINDS: frozenset[str]` and `is_known_job_kind(kind: str) -> bool` in `application/pipeline.py`. `application/pipeline.py` is a leaf module: the coordinators already import `queue_for_job_kind` from it, so `pipeline.py` **must not import any coordinator, worker, or `domain.publishing` module** — that would be a circular import. `KNOWN_JOB_KINDS` is a literal canonical `frozenset` written out in `pipeline.py` (`"ingest_youtube"`, `"transcribe"`, `"select_clips"`, `"render_clip"`, `"publish.instagram_reels"`, `"publish.youtube_shorts"`). The `*_JOB_KIND` string constants and `Platform.job_kind` values elsewhere continue to hold the same literals; the test in Step 1 is what proves they stay in sync.
- Changes: `worker._process_payload` fails an unknown-kind job inside the lock-holding session (session A) with `START` then `FAIL`, commits, and returns so the receipt is acknowledged. `_fail_job` keeps its second-session form only for the post-`rollback` exception path.
- Changes: worker startup asserts the handler map keys equal `KNOWN_JOB_KINDS` (raises `RuntimeError` naming the difference).
- Changes: `POST /api/v1/jobs/{job_id}/retry` returns HTTP 409 naming the kind when `not is_known_job_kind(job.kind)`, before any transition.

- [ ] **Step 1: Write failing tests**
  - `tests/test_pipeline.py`: `is_known_job_kind` true for each of the six kinds and false for `"legacy_unregistered_kind"`. A separate test imports `INGEST_YOUTUBE_JOB_KIND`, `TRANSCRIBE_JOB_KIND`, `SELECT_CLIPS_JOB_KIND`, `RENDER_CLIP_JOB_KIND` from their coordinator modules and both `Platform.job_kind` values from `domain.publishing`, and asserts `KNOWN_JOB_KINDS == {those six strings}` — the test crosses the layer boundary that production `pipeline.py` must not. (The circular-import constraint is not tested by inspecting source; it is enforced in Step 3 and confirmed in review — a violation would surface as an `ImportError` when the suite loads.)
  - `tests/integration/test_unknown_job_kind.py` (`pytestmark = pytest.mark.integration`): using the `session_factory` fixture over an engine created with `connect_args={"options": "-c lock_timeout=3000"}`, seed a `QUEUED` `JobRecord(kind="legacy_unregistered_kind")` with a `DELIVERED` outbox row; run `_process_payload(job_id, factory, handlers={})` inside a `ThreadPoolExecutor` future with `future.result(timeout=30)`; assert the future returns (no hang), the job is `FAILED` with `error == "UnknownJobKindError: legacy_unregistered_kind"`, and a second `_process_payload` call is a no-op.
  - `tests/test_api.py`: add `test_retry_rejects_unregistered_kind` — seed a `FAILED` job with kind `"legacy_unregistered_kind"`, `POST /api/v1/jobs/{id}/retry` with admin auth returns 409 and the body names the kind; the job stays `FAILED`.

- [ ] **Step 2: RED** — `pytest tests/test_pipeline.py tests/test_api.py -q` and `PG pytest tests/integration/test_unknown_job_kind.py -q`. Expected: FAIL (registry missing; retry returns 200; `_process_payload` hangs then times out).

- [ ] **Step 3: Add the registry** — write the literal `KNOWN_JOB_KINDS` frozenset and `is_known_job_kind` in `application/pipeline.py` with no new imports (the module stays a leaf that coordinators import from, never the reverse).

- [ ] **Step 4: Fix the worker** — in `_process_payload`, replace the `_fail_job(session_factory, …)` call in the unknown-kind branch with in-session `jobs.transition(job.id, JobEvent.START)`, `jobs.transition(job.id, JobEvent.FAIL, error=message)`, `session.commit()`, `return`. Add the startup handler-coverage assertion in `build_handlers` (or `run`).

- [ ] **Step 5: Guard retry** in `api/routes.py::retry_job` with `is_known_job_kind` before `jobs.retry_dispatched`.

- [ ] **Step 6: GREEN + gates**
  - `pytest tests/test_pipeline.py tests/test_api.py -q` → PASS.
  - `PG pytest tests/integration/test_unknown_job_kind.py -q` → PASS (also runs under `./scripts/verify.sh`).
  - `ruff check src tests && mypy src` → PASS.

- [ ] **Step 7: Commit** — `fix: fail unknown job kinds in the locking session`

---

### Task 2: Media-root containment with zero external mutation

**Files:**
- Modify: `src/openclips/infrastructure/media_storage.py`
- Modify: `tests/test_media_storage.py`

**Interfaces:**
- Produces: private `MediaStorage._prepare_target(key: str) -> Path` — the only path either write method uses to materialize a destination directory. It validates the key, resolves the root once, then walks intermediate components one at a time: reject an existing component that `is_symlink()` and resolves outside the root; otherwise `mkdir(exist_ok=True)` that single component (never `parents=True`) and re-check it; descend only after the component is proven contained; reject a final target that already exists as an escaping symlink; return the target.
- Produces: `MediaStorage.delete(key: str) -> bool` — reuses the containment walk, refuses to unlink through an escaping symlink, returns `True` when a file was removed and `False` when nothing existed.
- Changes: `write_stream` and `promote_file` call `_prepare_target` and never call `mkdir(parents=True)`.

- [ ] **Step 1: Write failing tests** in `tests/test_media_storage.py`:
  - `test_write_stream_rejection_leaves_outside_tree_byte_for_byte_unchanged`: create `tmp_path/outside` with nested content, snapshot it recursively (paths + bytes), symlink `media_root/escape -> outside`, call `storage.write_stream("escape/nested/payload.bin", [CONTENT])`, assert `UnsafeMediaPathError`, assert the recursive snapshot is identical and `outside/nested` was never created.
  - `test_promote_file_rejection_creates_nothing_outside_root`: same shape with a deep key `escape/a/b/c.bin` and a real temp file.
  - `test_delete_removes_contained_file_and_reports`: write a key, `delete` it returns `True`, file gone; second `delete` returns `False`.
  - `test_delete_refuses_to_unlink_through_escaping_symlink`: symlink `media_root/escape -> outside` containing `secret.bin`; `delete("escape/secret.bin")` raises `UnsafeMediaPathError` and `outside/secret.bin` still exists.
  - Keep every existing test in the file passing.

- [ ] **Step 2: RED** — `pytest tests/test_media_storage.py -q`. Expected: FAIL (`delete` missing; `nested` dir gets created before rejection).

- [ ] **Step 3: Implement** `_prepare_target` and `delete`; route `write_stream`/`promote_file` through `_prepare_target`; delete `_validate_destination`.

- [ ] **Step 4: GREEN + gates** — `pytest tests/test_media_storage.py -q` → PASS; `ruff check src tests && mypy src` → PASS.

- [ ] **Step 5: Commit** — `fix: contain media directory creation within the media root`

---

### Task 3: Redis FIFO restoration priority

**Files:**
- Modify: `src/openclips/infrastructure/queue.py`
- Modify: `tests/test_queue.py`
- Modify: `tests/integration/test_redis_dispatch.py`

**Interfaces:**
- Changes: `RedisJobQueue.restore_processing` moves from the tail of `processing` onto the head of `ready` in a loop (`LMOVE <processing> <ready> RIGHT LEFT`).
- Changes: `InMemoryJobQueue.restore_processing` mirrors it with `self._ready[q].appendleft(self._processing[q].pop())`.

- [ ] **Step 1: Write failing contract tests** (same body for both implementations):
  - `tests/test_queue.py::test_restore_processing_preserves_fifo_priority`: `enqueue("default","job-1"); enqueue("default","job-2"); claim; claim; enqueue("default","job-3"); restore_processing("default")`; then three `claim` calls yield payloads `"job-1"`, `"job-2"`, `"job-3"` in that exact order.
  - `tests/integration/test_redis_dispatch.py::test_restore_processing_preserves_fifo_priority_on_real_redis`: identical against `RedisJobQueue` on the reserved logical database.

- [ ] **Step 2: RED** — `pytest tests/test_queue.py -q` and `REDIS pytest tests/integration/test_redis_dispatch.py -q`. Expected: FAIL (recovered work lands after `job-3`).

- [ ] **Step 3: Implement** the `RIGHT LEFT` / `appendleft(pop())` reversal in both classes.

- [ ] **Step 4: GREEN + gates** — both commands PASS; `ruff check src tests && mypy src` → PASS.

- [ ] **Step 5: Commit** — `fix: restore recovered redis work ahead of ready backlog`

---

## Part 2 — V1 completion

### Task 4: Bounded worker concurrency and per-stage limits

**Files:**
- Create: `src/openclips/application/concurrency.py`
- Modify: `src/openclips/config.py`
- Modify: `src/openclips/worker.py`
- Modify: `.env.example`
- Create: `tests/test_concurrency.py`
- Create: `tests/integration/test_worker_concurrency.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces: `StageLimiter(limits: dict[str, int])` with `@contextmanager limit(stage: str)` acquiring a named `BoundedSemaphore` and releasing it in `finally`; unknown stage is a pass-through.
- Produces: `Settings.max_concurrent_transcriptions: int = Field(default=1, ge=1)` and `Settings.max_concurrent_renders: int = Field(default=1, ge=1)`, plus a model validator raising `ValueError` naming the offending field when either exceeds `worker_concurrency`.
- Changes: `worker.run` owns `ThreadPoolExecutor(max_workers=worker_concurrency)` and `BoundedSemaphore(worker_concurrency)`; the claim loop acquires a permit before `queue.claim`, submits `_process_payload` + `queue.ack` to the pool as one unit of work, and releases the permit when that future completes. `KeyboardInterrupt` stops claiming, waits for in-flight futures, then exits.
- Changes: `make_transcribe_handler` and `make_render_handler` wrap their coordinator `run` in `stage_limiter.limit("transcribe")` / `stage_limiter.limit("render_clip")`.
- Unchanged: `process_once` stays a single-shot helper for existing tests.

- [ ] **Step 1: Write failing tests**
  - `tests/test_concurrency.py`: with `StageLimiter({"transcribe": 1})`, a second `limit("transcribe")` from another thread blocks until the first context exits (prove with an `Event` and a short `join` timeout); `limit("unlimited")` never blocks; a raising body still releases the permit.
  - `tests/test_config.py`: `Settings(_env_file=None, worker_concurrency=2, max_concurrent_renders=2)` is valid; `max_concurrent_renders=3` raises `ValueError` mentioning `max_concurrent_renders`.
  - `tests/integration/test_worker_concurrency.py` (`pytest.mark.integration`, real PostgreSQL + `InMemoryJobQueue`): register a handler that increments a shared counter under a lock, records the max observed value, sleeps on a barrier, and decrements; enqueue `worker_concurrency + 3` jobs; run the bounded pool loop until drained; assert the recorded max never exceeds `worker_concurrency`, and with `max_concurrent_renders=1` the recorded max for `render_clip` handlers is `1`.

- [ ] **Step 2: RED** — `pytest tests/test_concurrency.py tests/test_config.py -q` and `PG pytest tests/integration/test_worker_concurrency.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement** `StageLimiter`, the settings + validator, and the bounded pool loop in `worker.run` (extract a testable `run_claim_loop(*, session_factory, handlers, queue, pool, permits, stop_event)` helper).

- [ ] **Step 4: Wire stage limits** into the two heavy handlers; construct one `StageLimiter` in `build_handlers`.

- [ ] **Step 5: GREEN + gates**
  - `pytest tests/test_concurrency.py tests/test_config.py -q` → PASS.
  - `PG pytest tests/integration/test_worker_concurrency.py -q` → PASS.
  - `pytest tests/test_publishing.py -q` (existing `process_once` test still green).
  - `ruff check src tests && mypy src` → PASS.

- [ ] **Step 6: Commit** — `feat: bound worker concurrency and per-stage limits`

---

### Task 5: Instagram public media URL provider and clip media endpoints

**Files:**
- Create: `src/openclips/providers/media_urls.py`
- Modify: `src/openclips/providers/platforms/base.py`
- Modify: `src/openclips/providers/platforms/instagram.py`
- Modify: `src/openclips/application/publishing.py`
- Modify: `src/openclips/application/services.py`
- Modify: `src/openclips/worker.py`
- Modify: `src/openclips/config.py`
- Modify: `src/openclips/api/routes.py`
- Modify: `src/openclips/api/schemas.py`
- Modify: `.env.example`
- Create: `tests/providers/test_media_urls.py`
- Create: `tests/test_clip_media_api.py`
- Modify: `tests/providers/test_platforms.py`
- Modify: `tests/test_publishing.py`

**Interfaces:**
- Produces in `providers/media_urls.py`: `PublicMediaUnavailableError(PublishError)`; `PublicMediaUrlProvider` protocol with `resolve(clip_id: UUID) -> str`; `BaseUrlMediaUrlProvider(base_url)` validating `http`/`https` scheme and non-empty host at construction and returning `f"{base_url.rstrip('/')}/api/v1/clips/{clip_id}/media"`; `UnavailableMediaUrlProvider` whose `resolve` always raises `PublicMediaUnavailableError` naming `OPENCLIPS_PUBLIC_MEDIA_BASE_URL`; `build_media_url_provider(base_url: str) -> PublicMediaUrlProvider` returning the unavailable provider when `base_url` is empty.
- Produces: `Settings.public_media_base_url: str = ""`.
- Changes: `PublishRequest` gains `media_url: str | None = None`. `InstagramReelsPublisher.publish` sends `request.media_url`, raises `PublishError` when it is `None`, and never builds a `file://` URL. `YouTubeShortsPublisher` untouched.
- Changes: `ScheduleCoordinator.__init__` gains `media_url_provider: PublicMediaUrlProvider | None = None`; `_request_for` sets `media_url = provider.resolve(record.clip_id)` only for `Platform.INSTAGRAM_REELS` (a `PublicMediaUnavailableError` propagates as a `PublishError` and is handled by `publish_publication._fail`).
- Produces: public `GET /api/v1/clips/{clip_id}/media` and `GET /api/v1/clips/{clip_id}/caption` — stream the artifact with `FileResponse`, resolved through `MediaStorage`, 404 when the clip has no `output_path` / `caption_path`.

- [ ] **Step 1: Write failing tests**
  - `tests/providers/test_media_urls.py`: `BaseUrlMediaUrlProvider("https://clips.example")` resolves `https://clips.example/api/v1/clips/<uuid>/media`; `"ftp://x"` and `"https://"` raise at construction; `UnavailableMediaUrlProvider().resolve(uuid4())` raises `PublicMediaUnavailableError` whose message contains `OPENCLIPS_PUBLIC_MEDIA_BASE_URL`; `build_media_url_provider("")` returns the unavailable provider.
  - `tests/providers/test_platforms.py`: `InstagramReelsPublisher` with a fake transport publishes when `request.media_url` is set and the posted JSON `video_url` equals it; raises `PublishError` when `media_url is None`; no argv or payload ever contains `file://`.
  - `tests/test_publishing.py`: with `media_url_provider=UnavailableMediaUrlProvider()`, publishing an Instagram publication fails the record with a reason naming the setting and never calls the transport.
  - `tests/test_clip_media_api.py`: a rendered clip returns 200 and the file bytes from `GET /api/v1/clips/{id}/media` (no auth header); an unrendered clip returns 404; caption endpoint mirrors it.

- [ ] **Step 2: RED** — `pytest tests/providers/test_media_urls.py tests/providers/test_platforms.py tests/test_publishing.py tests/test_clip_media_api.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement** the provider module, `PublishRequest.media_url`, the Instagram change, the coordinator wiring (`build_services` builds the provider from `settings.public_media_base_url` and exposes it on `AppServices`; `worker.make_publish_handlers` passes it into `ScheduleCoordinator`).

- [ ] **Step 4: Implement** the two `FileResponse` routes in `build_router`.

- [ ] **Step 5: GREEN + gates** — the Step 2 command PASS; `pytest tests/test_api.py -q` PASS; `ruff check src tests && mypy src` PASS.

- [ ] **Step 6: Commit** — `feat: resolve instagram media through a public url provider`

---

### Task 6: Atomic, idempotent publication dispatch and lifecycle

**Files:**
- Modify: `src/openclips/domain/publishing.py`
- Modify: `src/openclips/infrastructure/repositories.py`
- Modify: `src/openclips/application/publishing.py`
- Create: `src/openclips/application/scheduler.py`
- Modify: `src/openclips/worker.py`
- Modify: `src/openclips/config.py`
- Create: `alembic/versions/0009_publication_dispatch.py`
- Modify: `.env.example`
- Create: `tests/domain/test_publishing.py`
- Modify: `tests/test_publishing.py`
- Create: `tests/integration/test_publication_dispatch.py`

**Interfaces:**
- Produces: `PublicationStatus.QUEUED`, `PublicationStatus.CANCELLED`; `PublicationEvent.ENQUEUE`, `PublicationEvent.CANCEL`. New `PublicationStateMachine._transitions`: `SCHEDULED --ENQUEUE--> QUEUED`, `QUEUED --START--> PUBLISHING`, `PUBLISHING --SUCCEED--> PUBLISHED`, `PUBLISHING --FAIL--> FAILED`, `FAILED --RETRY--> SCHEDULED`, `{SCHEDULED,QUEUED,FAILED} --CANCEL--> CANCELLED`. `SCHEDULED --START--> PUBLISHING` is removed.
- Produces: `PublicationRepository.claim_due(now: datetime, limit: int) -> list[PublicationRecord]` — `status == SCHEDULED AND scheduled_at <= now`, `order_by(scheduled_at)`, `with_for_update(skip_locked=True)`, `limit(limit)`. `PublicationRepository.due` is removed.
- Changes: `PublicationRepository.transition` handles `ENQUEUE` and `CANCEL` (no `attempts` change).
- Changes: `ScheduleCoordinator.enqueue_due()` runs one transaction: `claim_due(clock(), limit)`, then per record `transition(id, ENQUEUE)` and `create_dispatched(platform.job_kind, payload=str(id), queue_name=platform.job_kind)`.
- Changes: `ScheduleCoordinator.publish_publication` requires `QUEUED` before `START`; a record found `CANCELLED` returns unchanged without contacting the platform.
- Produces: `PublicationScheduler(session_factory, clock, poll_interval_seconds, limit=50)` in `application/scheduler.py` with `dispatch_once() -> int` (opens a session, builds a `ScheduleCoordinator`, calls `enqueue_due`, commits, returns the job count), mirroring `OutboxRelay`.
- Changes: `worker.run` constructs a `PublicationScheduler` and calls `dispatch_once()` at most once per `OPENCLIPS_SCHEDULE_POLL_INTERVAL_SECONDS` (default `30`) alongside `relay.dispatch_once()`.
- Produces: migration `0009_publication_dispatch` adding `ix_publication_records_due` on `publication_records (status, scheduled_at)`; defensive `downgrade`. No column alteration (`status` is already `String(32)`).

- [ ] **Step 1: Write failing tests**
  - `tests/domain/test_publishing.py`: every allowed transition returns the expected status; `SCHEDULED --START-->` now raises `InvalidTransitionError`; `QUEUED --CANCEL--> CANCELLED`; `PUBLISHED --CANCEL-->` raises.
  - `tests/test_publishing.py`: update the existing flow — scheduling then `enqueue_due` moves the record to `QUEUED` and creates exactly one job + pending outbox; a second `enqueue_due` creates nothing; `publish_publication` on a `SCHEDULED` (not yet queued) record raises `InvalidTransitionError`; a `CANCELLED` record returns unchanged and the fake publisher records zero calls; failure path (`PUBLISHING -> FAILED -> RETRY -> SCHEDULED` with backed-off `scheduled_at`) and the five-attempt budget are unchanged.
  - `tests/integration/test_publication_dispatch.py` (real PostgreSQL): seed two due publications; two `Session`s each call `claim_due(now, 10)` — assert the claimed id sets are disjoint and together cover both; run `enqueue_due` twice in sequence and assert exactly one publish job plus one pending outbox row per publication and that both records are `QUEUED`; a redelivered publish message for an already-`PUBLISHED` publication is acknowledged and changes nothing.

- [ ] **Step 2: RED** — `pytest tests/domain/test_publishing.py tests/test_publishing.py -q` and `PG pytest tests/integration/test_publication_dispatch.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement** the lifecycle enums + state machine, `claim_due`, the transactional `enqueue_due`, and the `publish_publication` guard.

- [ ] **Step 4: Implement** `PublicationScheduler` and the worker-loop call + setting.

- [ ] **Step 5: Migration** — write `0009_publication_dispatch.py`; run the round trip against a disposable database (`upgrade head` / `check` / `downgrade -1` / `upgrade head`).

- [ ] **Step 6: GREEN + gates**
  - `pytest tests/domain/test_publishing.py tests/test_publishing.py -q` → PASS.
  - `PG pytest tests/integration/test_publication_dispatch.py -q` → PASS.
  - Migration round trip clean; `alembic check` → `No new upgrade operations detected.`
  - `ruff check src tests && mypy src` → PASS.

- [ ] **Step 7: Commit** — `feat: dispatch due publications atomically and idempotently`

---

### Task 7: Independent per-platform scheduling rules

**Files:**
- Modify: `src/openclips/application/publishing.py`
- Modify: `src/openclips/infrastructure/repositories.py`
- Modify: `src/openclips/application/services.py`
- Modify: `src/openclips/config.py`
- Modify: `.env.example`
- Modify: `tests/test_publishing.py`
- Create: `tests/integration/test_schedule_watermark.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces: `parse_daily_window(value: str) -> DailyWindowRule` in `application/publishing.py`, accepting comma-separated `HH:MM` UTC times and raising `ValueError` naming the offending string.
- Produces: `Settings.instagram_schedule_times: str = "13:00,18:30"`, `Settings.youtube_schedule_times: str = "16:00"`.
- Produces: `PlatformScheduleRules = dict[Platform, DailyWindowRule]`; `build_services` parses both settings into it and `AppServices` exposes `schedule_rules`.
- Produces: `PublicationRepository.latest_scheduled_at(platform: Platform) -> datetime | None` — the max `scheduled_at` across non-`CANCELLED` publications for that platform.
- Changes: `ScheduleCoordinator.__init__` gains `schedule_rules: PlatformScheduleRules | None = None`. `schedule(clip_id, platform, scheduled_at=None)`: when `scheduled_at is None` and a rule exists, `anchor = max(clock(), latest_scheduled_at(platform) or clock())` and `scheduled_at = rule.next_slot(anchor)` (strictly after `anchor`); when `scheduled_at` is given it bypasses the rule and naive timestamps are read as UTC. Instagram and YouTube never share a watermark.

- [ ] **Step 1: Write failing tests**
  - `tests/test_config.py`: defaults parse; `Settings(_env_file=None, instagram_schedule_times="25:00")` → building services raises `ValueError` mentioning `"25:00"`.
  - `tests/test_publishing.py`: with a frozen clock and `schedule_rules={INSTAGRAM_REELS: DailyWindowRule((time(13,0), time(18,30)))}`, three successive `schedule(clip, INSTAGRAM_REELS)` calls land on `13:00`, `18:30`, next-day `13:00` (watermark advances); a YouTube `schedule` in between is unaffected; an explicit `scheduled_at` bypasses the rule.
  - `tests/integration/test_schedule_watermark.py` (real PostgreSQL): `latest_scheduled_at` returns `None` with no rows, then the newest non-cancelled `scheduled_at`; cancelling that publication moves the watermark back.

- [ ] **Step 2: RED** — `pytest tests/test_config.py tests/test_publishing.py -q` and `PG pytest tests/integration/test_schedule_watermark.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement** `parse_daily_window`, the settings, `latest_scheduled_at`, the `AppServices` field, and the `schedule` watermark logic.

- [ ] **Step 4: GREEN + gates** — Step 2 commands PASS; `ruff check src tests && mypy src` → PASS.

- [ ] **Step 5: Commit** — `feat: schedule publications per platform by daily window rule`

---

### Task 8: Cancel live publications when a clip is edited

**Files:**
- Modify: `src/openclips/application/publishing.py`
- Modify: `src/openclips/infrastructure/repositories.py`
- Modify: `src/openclips/api/routes.py`
- Modify: `tests/test_publishing.py`
- Modify: `tests/test_api.py`

**Interfaces:**
- Produces: `PublicationRepository.cancel_active_for_clip(clip_id: UUID) -> int` — transitions every `SCHEDULED` or `QUEUED` publication for that clip with `CANCEL`, leaves `PUBLISHED`/`FAILED`/`CANCELLED` untouched, returns the count.
- Produces: `ScheduleCoordinator.cancel_for_clip(clip_id: UUID) -> int` delegating to the repository (works with `publishers={}`).
- Changes: every clip-edit path in `api/routes.py` (`PATCH /clips/{id}`, `PUT /clips/{id}/caption-edits`) calls `ScheduleCoordinator.cancel_for_clip` in the same request session, before the `ClipEvent.EDIT` transition. `PUBLISHED` clip history is never rewritten.

- [ ] **Step 1: Write failing tests**
  - `tests/test_publishing.py::test_cancel_for_clip_cancels_scheduled_and_queued_only`: one `SCHEDULED`, one `QUEUED`, one `PUBLISHED` publication for a clip; `cancel_for_clip` returns `2`, the first two are `CANCELLED`, the `PUBLISHED` one is unchanged.
  - `tests/test_api.py::test_editing_scheduled_clip_cancels_publications`: approve + schedule a clip (status `SCHEDULED`), `PATCH /clips/{id}` with a new `title` and admin auth → 200, clip is `NEEDS_REVIEW`, its publication is `CANCELLED`; a later scheduler pass creates no job for it.

- [ ] **Step 2: RED** — `pytest tests/test_publishing.py tests/test_api.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement** `cancel_active_for_clip`, `cancel_for_clip`, and the route wiring (build a minimal `ScheduleCoordinator` in `build_router` edit handlers, or call the repository directly through the coordinator method).

- [ ] **Step 4: GREEN + gates** — `pytest tests/test_publishing.py tests/test_api.py -q` PASS; `ruff check src tests && mypy src` PASS.

- [ ] **Step 5: Commit** — `fix: cancel live publications when a clip is edited`

---

### Task 9: Platform-specific post copy

**Files:**
- Create: `src/openclips/domain/copy.py`
- Create: `src/openclips/application/copy.py`
- Modify: `src/openclips/infrastructure/models.py`
- Modify: `src/openclips/infrastructure/repositories.py`
- Modify: `src/openclips/application/publishing.py`
- Modify: `src/openclips/worker.py`
- Create: `alembic/versions/0010_clip_platform_copy.py`
- Create: `tests/test_copy.py`
- Create: `tests/integration/test_clip_copy_repository.py`
- Modify: `tests/test_publishing.py`

**Interfaces:**
- Produces in `domain/copy.py`: `INSTAGRAM_CAPTION_LIMIT = 2200`, `YOUTUBE_TITLE_LIMIT = 100`, `YOUTUBE_DESCRIPTION_LIMIT = 5000`; `CopyTooLongError(ValueError)` carrying `platform`, `field`, `actual`, `limit`; `validate_copy(platform: Platform, title: str, description: str) -> None`. Instagram is validated as `f"{title}\n\n{description}".strip()` length against `INSTAGRAM_CAPTION_LIMIT`; YouTube validates `title` and `description` separately.
- Produces: `ClipPlatformCopyRecord` mapped to `clip_platform_copy (id, clip_id FK, platform, title, description, created_at, updated_at)` with a unique constraint on `(clip_id, platform)`.
- Produces: `ClipCopyRepository` with `upsert(clip_id, platform, *, title, description) -> ClipPlatformCopyRecord` (validates via `validate_copy` first), `get(clip_id, platform) -> … | None`, `list_for_clip(clip_id) -> list[…]`.
- Produces: `CopyComposer.compose(clip: ClipRecord, document: TranscriptDocument) -> dict[Platform, tuple[str, str]]` — deterministic default copy from `clip.title` and the first sentences of the clip's transcript window, truncated on a word boundary to each platform's limit. No network.
- Changes: `ScheduleCoordinator.__init__` gains `copy: ClipCopyRepository | None = None`; `_request_for` builds `PublishRequest` from the stored copy for `record.platform`, falling back to `clip.title` and `""` when no row exists. `worker.make_publish_handlers` passes `ClipCopyRepository(session)`.
- Produces: migration `0010_clip_platform_copy`; defensive `downgrade`.

- [ ] **Step 1: Write failing tests**
  - `tests/test_copy.py`: `validate_copy(Platform.YOUTUBE_SHORTS, "x"*101, "")` raises `CopyTooLongError` with `field == "title"`, `actual == 101`, `limit == 100`; Instagram joined-length check; `CopyComposer.compose` is byte-identical across two calls on the same clip + document and each field is within its limit and ends on a word boundary.
  - `tests/integration/test_clip_copy_repository.py` (real PostgreSQL): `upsert` twice for the same `(clip, platform)` updates in place (one row); a different platform is a second row; `list_for_clip` returns both.
  - `tests/test_publishing.py`: with a stored Instagram copy row, `publish_publication` sends `PublishRequest(title=<stored title>, description=<stored description>)`; with no row it falls back to `clip.title`.

- [ ] **Step 2: RED** — `pytest tests/test_copy.py tests/test_publishing.py -q` and `PG pytest tests/integration/test_clip_copy_repository.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement** `domain/copy.py`, the ORM record, `ClipCopyRepository`, `CopyComposer`, and the coordinator wiring.

- [ ] **Step 4: Migration** — `0010_clip_platform_copy.py`; run the round trip; `alembic check` clean.

- [ ] **Step 5: GREEN + gates** — Step 2 commands PASS; migration round trip clean; `ruff check src tests && mypy src` PASS.

- [ ] **Step 6: Commit** — `feat: compose and store platform-specific post copy`

---

### Task 10: Scheduling, publication, and copy API

**Files:**
- Create: `src/openclips/api/publishing_routes.py`
- Modify: `src/openclips/api/routes.py`
- Modify: `src/openclips/api/schemas.py`
- Modify: `src/openclips/application/services.py`
- Create: `tests/test_publishing_api.py`

**Interfaces:**
- Produces: `build_publishing_router(*, get_session, require_admin, services) -> APIRouter`, included by `build_router` (`router.include_router(...)`).
- Produces schemas: `PublicationOut`, `ScheduleClipBody{platform, scheduled_at?}`, `BulkScheduleBody{clip_ids, platform, scheduled_at?}`, `BulkPublicationResultItem{clip_id, ok, publication_id?, error?}`, `ClipCopyOut`, `ClipCopyBody{title, description}`.
- Produces endpoints (mutations require the admin bearer token; reads public):
  - `POST /api/v1/clips/{clip_id}/schedule` → `PublicationOut`
  - `POST /api/v1/clips/bulk-schedule` → `list[BulkPublicationResultItem]`
  - `GET /api/v1/publications?platform=&publication_status=&limit=`
  - `GET /api/v1/publications/{publication_id}`
  - `POST /api/v1/publications/{publication_id}/retry`
  - `POST /api/v1/publications/{publication_id}/cancel`
  - `PUT /api/v1/clips/{clip_id}/copy/{platform}` → `ClipCopyOut`
  - `GET /api/v1/clips/{clip_id}/copy` → `list[ClipCopyOut]`
- Status codes: 404 unknown clip/publication; 409 for `ClipNotApprovedError`, `SchedulingExhaustedError`, `InvalidTransitionError`, and Instagram scheduling while `public_media_base_url` is empty (checked before creating the record, same actionable message as `PublicMediaUnavailableError`); 422 for `CopyTooLongError`. Bulk returns one item per clip.

- [ ] **Step 1: Write failing contract tests** in `tests/test_publishing_api.py` (SQLite `TestClient` like `tests/test_api.py`):
  - unauthenticated `POST …/schedule` → 401; unknown clip → 404; non-`APPROVED` clip → 409.
  - approved clip, no `scheduled_at`, with a configured rule → 200 and `scheduled_at` equals the rule slot; explicit `scheduled_at` → 200 and honored; a second approved clip on the same platform lands on the next slot; a YouTube schedule is independent.
  - Instagram schedule with empty `public_media_base_url` → 409 naming `OPENCLIPS_PUBLIC_MEDIA_BASE_URL`.
  - drive a publication to `FAILED`, `POST …/retry` → 200 and `SCHEDULED`; after the 5-attempt budget → 409; `POST …/cancel` on a `SCHEDULED` record → 200 and `CANCELLED`.
  - `PUT …/copy/INSTAGRAM_REELS` with an over-limit body → 422; a valid body → 200 and `GET …/copy` returns it.
  - `POST /clips/bulk-schedule` returns one result per clip with `ok` and `publication_id` or `error`.

- [ ] **Step 2: RED** — `pytest tests/test_publishing_api.py -q`. Expected: FAIL (router not mounted).

- [ ] **Step 3: Implement** the schemas, the router, the `build_router` include, and any `AppServices` accessors the router needs (`schedule_rules`, `media_url_provider`, `public_media_base_url`).

- [ ] **Step 4: GREEN + gates** — `pytest tests/test_publishing_api.py tests/test_api.py -q` PASS; `ruff check src tests && mypy src` PASS.

- [ ] **Step 5: Commit** — `feat: expose scheduling, publication, and copy endpoints`

---

### Task 11: YouTube channel registration and polling

**Files:**
- Modify: `src/openclips/providers/youtube.py`
- Create: `src/openclips/domain/channels.py`
- Create: `src/openclips/application/channels.py`
- Modify: `src/openclips/infrastructure/models.py`
- Modify: `src/openclips/infrastructure/repositories.py`
- Create: `src/openclips/api/channel_routes.py`
- Modify: `src/openclips/api/routes.py`
- Modify: `src/openclips/api/schemas.py`
- Modify: `src/openclips/application/services.py`
- Modify: `src/openclips/worker.py`
- Modify: `src/openclips/config.py`
- Create: `alembic/versions/0011_youtube_channels.py`
- Modify: `.env.example`
- Create: `tests/test_channels.py`
- Create: `tests/test_channel_api.py`
- Create: `tests/integration/test_channel_polling.py`

**Interfaces:**
- Produces in `providers/youtube.py`: `extract_youtube_channel_id(url) -> str` accepting `/channel/UC…`, `/@handle`, `/c/<name>`, `/user/<name>` and rejecting everything else with `UnsupportedMediaLocator`; `canonicalize_youtube_channel_url(url) -> str`; `YtDlpChannelLister` building shell-free argv `yt-dlp --flat-playlist --print id -I 1:N -- <url>` with a bounded timeout and truncated stderr tail, exposing `list_video_ids(url: str, limit: int) -> list[str]`.
- Produces in `domain/channels.py`: `is_due(last_polled_at: datetime | None, poll_interval_seconds: int, now: datetime) -> bool`.
- Produces: `YouTubeChannelRecord` → `youtube_channels (id, channel_url, external_id UNIQUE, poll_interval_seconds, enabled, auto_process, last_polled_at, last_error, created_at, updated_at)`; `YouTubeChannelRepository` with `register`, `get`, `list_all`, `update_settings`, `mark_polled(id, *, error: str | None)`.
- Produces in `application/channels.py`: `ChannelIngestionCoordinator.register(url, *, auto_process, poll_interval_seconds) -> YouTubeChannelRecord` (canonicalizes, reuses by `external_id`, never downloads); `ChannelPoller.poll_due(now) -> int` — for each enabled due channel, list up to `OPENCLIPS_CHANNEL_POLL_MAX_VIDEOS` ids and register each new one through `YouTubeIngestionCoordinator.register`, skipping any whose `youtube_idempotency_key(video_id)` already exists and catching `IntegrityError` per video as "already registered"; `last_polled_at` advances on every completed poll including empty ones; a lister failure records `last_error`, advances `last_polled_at`, and does not abort remaining channels.
- Produces: `Settings.channel_poll_interval_seconds: int = 3600`, `Settings.channel_poll_max_videos: int = Field(default=10, ge=1)`.
- Produces: `build_channel_router(*, get_session, require_admin, services)` with `POST /api/v1/channels`, `GET /api/v1/channels`, `PATCH /api/v1/channels/{channel_id}`, `POST /api/v1/channels/{channel_id}/poll`; included by `build_router`.
- Changes: `worker.run` calls `ChannelPoller.poll_due(now)` at most once per `channel_poll_interval_seconds`.
- Produces: migration `0011_youtube_channels`; defensive `downgrade`.

- [ ] **Step 1: Write failing tests**
  - `tests/test_channels.py`: `extract_youtube_channel_id` for each accepted form and rejection of a bare video URL; `is_due(None, 3600, now)` is `True`; `is_due(now - 10s, 3600, now)` is `False`; `ChannelIngestionCoordinator.register` with a fake lister reuses a record by `external_id`; a `ChannelPoller` run with a fake lister yielding `["a","b"]` registers two sources, and a second pass with `["a","b","c"]` registers only `c`; a lister that raises records `last_error` and still advances `last_polled_at`, and a second channel in the same pass still polls.
  - `tests/test_channel_api.py`: `POST /api/v1/channels` needs admin auth; `GET` is public; `PATCH` toggles `enabled`/`auto_process`/interval; `POST …/poll` triggers an immediate poll and returns the count.
  - `tests/integration/test_channel_polling.py` (real PostgreSQL): repeated `poll_due` passes over a fake lister create exactly one `source_assets` row and one `ingest_youtube` job per video id; a concurrent duplicate insert raising `IntegrityError` is swallowed.

- [ ] **Step 2: RED** — `pytest tests/test_channels.py tests/test_channel_api.py -q` and `PG pytest tests/integration/test_channel_polling.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement** the provider helpers, `domain/channels.py`, the ORM record + repository, `application/channels.py`, the router, the worker-loop call, and settings.

- [ ] **Step 4: Migration** — `0011_youtube_channels.py`; round trip; `alembic check` clean.

- [ ] **Step 5: GREEN + gates** — Step 2 commands PASS; migration round trip clean; `ruff check src tests && mypy src` PASS.

- [ ] **Step 6: Commit** — `feat: register and poll youtube channels`

---

### Task 12: Source retention sweep

**Files:**
- Modify: `src/openclips/infrastructure/models.py`
- Create: `src/openclips/domain/retention.py`
- Create: `src/openclips/application/retention.py`
- Modify: `src/openclips/infrastructure/repositories.py`
- Modify: `src/openclips/worker.py`
- Modify: `src/openclips/config.py`
- Create: `alembic/versions/0012_source_retention.py`
- Modify: `.env.example`
- Create: `tests/test_retention.py`
- Create: `tests/integration/test_retention_sweeper.py`

**Interfaces:**
- Produces: `source_assets.media_purged_at: datetime | None`.
- Produces in `domain/retention.py`: `is_purgeable(*, status: SourceStatus, retain_until: datetime, media_path: str | None, media_purged_at: datetime | None, now: datetime) -> bool` — `True` only when the source is `READY`, `now >= retain_until`, `media_path` is set, and `media_purged_at` is `None`.
- Produces: `SourceRepository.retention_candidates(now) -> list[SourceAssetRecord]`, `SourceRepository.mark_media_purged(source_id, purged_at)` (sets `media_purged_at`, clears `media_path`), plus guard queries `has_active_job_for_source(source_id) -> bool` (any job with that source id in `payload` is `QUEUED`/`RUNNING`) and `has_incomplete_clips(source_id) -> bool` (any clip lacking `output_path` and not `REJECTED`).
- Produces: `RetentionSweeper.sweep(now) -> int` — for each candidate that passes both guards, `MediaStorage.delete(media_path)`, `mark_media_purged`, count it. Clip `output_path`/`caption_path` are never touched; no clip or source row is deleted.
- Produces: `Settings.retention_sweep_interval_seconds: int = 3600`.
- Changes: `worker.run` calls `RetentionSweeper.sweep(now)` at most once per `retention_sweep_interval_seconds`.
- Produces: migration `0012_source_retention`; defensive `downgrade`.

- [ ] **Step 1: Write failing tests**
  - `tests/test_retention.py`: `is_purgeable` truth table — `READY` + expired + `media_path` + no `media_purged_at` → `True`; each missing precondition → `False`.
  - `tests/integration/test_retention_sweeper.py` (real PostgreSQL + real `MediaStorage` on `tmp_path`): a `READY` source with `retain_until` in the past and a written media file plus a rendered clip (`output_path` set) → `sweep` returns `1`, the source media file is gone, `media_purged_at` is set, `media_path` is cleared, and the clip's `output_path`/`caption_path` files still exist; a source with a `RUNNING` job is skipped; a source with a clip missing `output_path` (not `REJECTED`) is skipped; a source not yet expired is skipped.

- [ ] **Step 2: RED** — `pytest tests/test_retention.py -q` and `PG pytest tests/integration/test_retention_sweeper.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement** the column, predicate, repository helpers, sweeper, worker-loop call, and setting.

- [ ] **Step 4: Migration** — `0012_source_retention.py`; round trip; `alembic check` clean.

- [ ] **Step 5: GREEN + gates** — Step 2 commands PASS; migration round trip clean; `ruff check src tests && mypy src` PASS.

- [ ] **Step 6: Commit** — `feat: purge expired source media on a retention sweep`

---

### Task 13: Real framing — focus crop and split screen

**Files:**
- Modify: `src/openclips/domain/clips.py`
- Modify: `src/openclips/providers/renderer.py`
- Modify: `src/openclips/application/rendering.py`
- Modify: `src/openclips/application/services.py`
- Modify: `src/openclips/worker.py`
- Modify: `src/openclips/infrastructure/models.py`
- Modify: `src/openclips/infrastructure/repositories.py`
- Modify: `src/openclips/api/routes.py`
- Modify: `src/openclips/api/schemas.py`
- Create: `alembic/versions/0013_clip_framing.py`
- Modify: `tests/providers/test_renderer.py`
- Modify: `tests/test_render_coordinator.py`
- Modify: `tests/test_api.py`
- Create: `tests/integration/test_split_screen_render.py`

**Interfaces:**
- Produces: `ClipLayout(StrEnum)` = `SINGLE`, `SPLIT` in `domain/clips.py`.
- Produces in `renderer.py`: `FocusCropStrategy(focus_x: float)` computing a real crop window (crop height = source height, crop width = `round(source_height * W / H)` clamped to source width with the symmetric fallback; both dims rounded down to even; `x = clamp(round(focus_x * source_width - crop_width / 2), 0, source_width - crop_width)`; `y = clamp((source_height - crop_height)//2, 0, source_height - crop_height)`; filter `crop=<cw>:<ch>:<x>:<y>`). `SingleCropLayout(focus_x)` and `SplitScreenLayout(top_focus_x, bottom_focus_x)`; `RenderLayout = SingleCropLayout | SplitScreenLayout`. `build_render_argv(request)` emits `-vf` for single crop (unchanged shape) and `-filter_complex` + `-map "[v]" -map "0:a?"` for split screen (`crop,scale=W:H2,setsar=1` per half then `vstack=inputs=2`), appending subtitles to the final filter node in both cases. `SpeakerCropStrategy`, `CenterCropStrategy`, `CropStrategy`, and `build_crop_filters` are removed.
- Changes: `RenderRequest` replaces `crop_filters: tuple[str, ...]` with `layout: RenderLayout`; `FFmpegRenderer.render(request: RenderRequest) -> list[str]` drops its `crop_strategy` argument (`probe_media` still supplies the source geometry the layout arithmetic needs).
- Changes: `RenderCoordinator` drops the `crop_strategy` parameter and builds the layout from the clip row (`clip.layout`, `clip.focus_x`, `clip.split_top_focus_x`, `clip.split_bottom_focus_x`); `AppServices` drops `crop_strategy`; `worker.make_render_handler` and `api/routes.py::enqueue_render` stop passing it.
- Produces: `clips.layout` (`SINGLE`/`SPLIT`, default `SINGLE`), `clips.focus_x` (default `0.5`), `clips.split_top_focus_x`, `clips.split_bottom_focus_x` (nullable). `ClipRepository.set_framing(clip_id, *, layout, focus_x, split_top_focus_x, split_bottom_focus_x)`.
- Changes: `ClipOut` and `ClipEditBody` gain the four framing fields plus `caption_template`; `edit_clip` validates each focus value to `[0.0, 1.0]` and `caption_template` against the six built-ins (`domain.captions.get_template`), returning 422 on violation, applies them via `set_framing` / `set_title`-style setters, then runs the shared edit path (→ `NEEDS_REVIEW`, cancel live publications from Task 8). `caption_template` on the clip row is the operator's chosen template; the renderer reads it instead of the settings singleton.
- Produces: migration `0013_clip_framing`; defensive `downgrade`.

- [ ] **Step 1: Write failing tests**
  - `tests/providers/test_renderer.py`: `FocusCropStrategy(0.5)` on `1920x1080` yields `crop=608:1080:656:0` (or the exact computed even values — assert the arithmetic, not a guess) and is equivalent to today's scale-then-center output; `FocusCropStrategy(0.0)` clamps `x` to `0`; `FocusCropStrategy(1.0)` clamps `x` to `source_width - crop_width`; assert exact argv for three source geometries; `SplitScreenLayout(0.3, 0.7)` produces the documented `-filter_complex` string with `vstack=inputs=2[v]` and `-map "[v]" -map "0:a?"`, and subtitles append to `[v]`.
  - `tests/test_render_coordinator.py`: a clip row with `layout=SPLIT` renders through the split-screen argv; a default clip (`focus_x=0.5`) renders through the single-crop argv.
  - `tests/test_api.py`: `PATCH /clips/{id}` with `focus_x=1.5` → 422; with `caption_template="nope"` → 422; with `layout="SPLIT", split_top_focus_x=0.2, split_bottom_focus_x=0.8, caption_template="podcast"` → 200, values persisted, clip `NEEDS_REVIEW`; a re-render of that clip resolves `get_template("podcast")`.
  - `tests/integration/test_split_screen_render.py` (real FFmpeg, skips if `shutil.which("ffmpeg")` is `None`): render a generated source with `SplitScreenLayout` and assert FFprobe reports `1080x1920`.

- [ ] **Step 2: RED** — `pytest tests/providers/test_renderer.py tests/test_render_coordinator.py tests/test_api.py -q` and `pytest tests/integration/test_split_screen_render.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement** the layout arithmetic and argv, the `RenderRequest` change, the coordinator/services/worker/route cleanup, the ORM columns + `set_framing`, and the schema/validation changes.

- [ ] **Step 4: Migration** — `0013_clip_framing.py`; round trip; `alembic check` clean.

- [ ] **Step 5: GREEN + gates**
  - Step 2 commands PASS.
  - `pytest tests/integration/test_real_render.py -q` still PASS (default framing unchanged).
  - Migration round trip clean.
  - `ruff check src tests && mypy src` PASS.

- [ ] **Step 6: Commit** — `feat: render operator focus crop and split screen layouts`

---

### Task 14: Functional review dashboard

**Files:**
- Modify: `pyproject.toml`
- Create: `src/openclips/api/templates/dashboard.html`
- Create: `src/openclips/api/static/dashboard.js`
- Create: `src/openclips/api/static/dashboard.css`
- Modify: `src/openclips/api/routes.py`
- Modify: `src/openclips/main.py`
- Create: `tests/test_dashboard.py`

**Interfaces:**
- Adds `jinja2>=3.1` to `[project].dependencies`; adds a hatch `force-include` (or `artifacts`) entry so `api/templates/**` and `api/static/**` ship in the wheel.
- Changes: `GET /api/v1/dashboard` renders `dashboard.html` through a `jinja2.Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"))`, resolved relative to `__file__`. `main.create_app` mounts `StaticFiles(directory=Path(api.__file__).parent / "static")` at `/static`.
- The page is server-rendered HTML + vanilla JS calling the documented JSON API. The browser holds the pasted admin token in `sessionStorage` and sends `Authorization: Bearer …` on mutations only. No cookie, no session store, no new credential, no build step, no SPA framework.
- Capabilities wired to existing endpoints: queue view (status filter), preview (`<video>` against `/clips/{id}/media` + metadata from `/clips/{id}`), edit (title, start/end, template from the six built-ins, subtitle word edits, framing), per-platform copy editors with remaining-character budgets, approve/reject with verbatim 409 text, bulk approve/reject/schedule, and a schedule panel (platform selector, optional date/time, publications list with status/scheduled time/attempts/error/external URL and retry + cancel actions).

- [ ] **Step 1: Write failing tests** in `tests/test_dashboard.py` (SQLite `TestClient`):
  - `GET /api/v1/dashboard` returns 200 `text/html`, the body references `/static/dashboard.js` and `/static/dashboard.css`, and it contains the titles of seeded reviewable clips.
  - `GET /static/dashboard.js` returns 200 `application/javascript` (or `text/javascript`).
  - Behavior is covered by the JSON contract tests from Tasks 10 and 13 — no browser automation.

- [ ] **Step 2: RED** — `pytest tests/test_dashboard.py -q`. Expected: FAIL (`jinja2` missing / static not mounted).

- [ ] **Step 3: Implement** the dependency + packaging, the template, the JS and CSS, the route, and the static mount.

- [ ] **Step 4: GREEN + gates** — `pytest tests/test_dashboard.py tests/test_api.py -q` PASS; `ruff check src tests && mypy src` PASS.

- [ ] **Step 5: Commit** — `feat: rebuild the review dashboard on the documented api`

---

### Task 15: Opt-in real-provider smoke gates

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/smoke/__init__.py`
- Create: `tests/smoke/test_real_whisper.py`
- Create: `tests/smoke/test_live_youtube.py`
- Create: `tests/smoke/test_sandbox_publish.py`
- Create: `scripts/smoke.sh`
- Create: `tests/test_smoke_gates.py`

**Interfaces:**
- Registers the `smoke` marker in `[tool.pytest.ini_options].markers` so `--strict-markers` passes. No `addopts` filter is added — the default gate reports these as skipped and `scripts/verify.sh` stays hermetic.
- Each smoke module declares `pytestmark = [pytest.mark.smoke, pytest.mark.skipif(<env absent>, reason=<actionable>)]`:
  - `test_real_whisper.py` — `OPENCLIPS_SMOKE_REAL_WHISPER=1`; generates an audio fixture with FFmpeg, runs `FasterWhisperProvider.transcribe`, asserts normalized word timings.
  - `test_live_youtube.py` — `OPENCLIPS_SMOKE_YOUTUBE_URL` and `OPENCLIPS_SMOKE_YOUTUBE_CHANNEL_URL`; a real video downloads and promotes; a real channel lists ids idempotently across two `YtDlpChannelLister` calls.
  - `test_sandbox_publish.py` — `OPENCLIPS_SMOKE_SANDBOX_PUBLISH=1` plus the operator's already-configured platform credentials; an adapter reaches a real sandbox endpoint and preserves its response.
- `scripts/smoke.sh` documents running them with operator credentials. No credential value is committed; CI is not given secrets.

- [ ] **Step 1: Write the gate test** `tests/test_smoke_gates.py`: `pytest --collect-only -q tests/smoke` collects three items; running `pytest tests/smoke -q` with none of the env vars set reports exactly three skips, each with a reason naming its enabling variable; `pytest --strict-markers` does not error on the `smoke` marker.

- [ ] **Step 2: RED** — `pytest tests/test_smoke_gates.py -q`. Expected: FAIL (modules and marker missing).

- [ ] **Step 3: Implement** the marker registration, the three smoke modules with real assertions behind `skipif`, and `scripts/smoke.sh` (`chmod +x`).

- [ ] **Step 4: GREEN + gates**
  - `pytest tests/test_smoke_gates.py -q` → PASS.
  - `pytest tests/smoke -q` → `3 skipped`, each reason actionable.
  - `pytest -q` (full local run) shows the three as skipped, no errors.
  - `ruff check src tests && mypy src` → PASS.

- [ ] **Step 5: Commit** — `test: add opt-in real-provider smoke gates`

---

### Task 16: Truthful documentation

**Files:**
- Create: `progress.md`
- Modify: `docs/PHASES.md`
- Modify: `README.md`
- Create: `tests/test_license_stays_open.py`

**Interfaces:**
- This task writes only stable prose and status — no verification numbers, counts, or "evidence" lines. Every concrete measurement is recorded by Task 17 after the commands run. `docs/VERIFICATION.md` is not touched here.
- `progress.md` at the repository root is the single status surface: each phase, what capability it is expected to deliver, and what is explicitly deferred — including the plain statement that **automatic active-speaker detection is not implemented** and V1 framing is manual and metadata-driven.
- `docs/PHASES.md` gains a "V1 completion" section describing the scope and structure of this subproject (the three blockers, the feature areas, the gate). Its Phase 6 entry gets one corrective sentence: the original Phase 6 gate covered components in isolation, and the scheduling API, production dispatcher, and atomic due-claim landed in V1 completion. No "Verification evidence" line is added to the new section — Task 17 appends that.
- `README.md` documents the new `OPENCLIPS_*` settings, the scheduling/publication/copy/channel/media endpoints, the dashboard, channel polling, retention, framing, and `scripts/smoke.sh`. No claim about live faster-whisper, live YouTube, or real platform publishing is written; those are only ever recorded from an executed smoke run.
- `tests/test_license_stays_open.py`: asserts no `LICENSE`, `LICENSE.md`, `LICENSE.txt`, or `COPYING` file exists at the repo root and that `pyproject.toml` `[project]` declares neither `license` nor `license-files`.

- [ ] **Step 1: Write the failing test** `tests/test_license_stays_open.py` as described.

- [ ] **Step 2: RED** — `pytest tests/test_license_stays_open.py -q`. Expected: PASS already if the tree is clean; if it fails, stop and surface the unexpected license artifact rather than deleting it.

- [ ] **Step 3: Write the docs** — `progress.md`, the `docs/PHASES.md` scope section and Phase 6 corrective sentence, and the `README.md` additions. State expected capability and configuration only; write no numeric result.

- [ ] **Step 4: Gate** — `pytest tests/test_license_stays_open.py -q` PASS; `ruff check src tests` PASS; no code changed so `mypy src` is unaffected.

- [ ] **Step 5: Commit** — `docs: record v1 completion status and keep the license open`

---

### Task 17: Final verification and whole-branch review

**Files:**
- Modify: `docs/VERIFICATION.md`
- Modify: `docs/PHASES.md`

**Interfaces:**
- Consumes every interface from Tasks 1–16. Produces no code change — only recorded evidence.

- [ ] **Step 1: Run the full gate** — `./scripts/verify.sh`. Expected: every pytest test passes (the opt-in smoke trio reported skipped); Ruff passes; strict `mypy src` passes; the HTTP smoke checks succeed.

- [ ] **Step 2: Migration round trips** — for each of `0009`–`0013`, against a disposable `openclips_test_$$` database: `alembic upgrade head` → `alembic check` (`No new upgrade operations detected.`) → `alembic downgrade -1` → `alembic upgrade head`.

- [ ] **Step 3: Real-dependency integration sweep** — `./scripts/verify.sh` already runs it; separately confirm `tests/integration/test_unknown_job_kind.py`, `test_redis_dispatch.py`, `test_worker_concurrency.py`, `test_publication_dispatch.py`, `test_channel_polling.py`, `test_retention_sweeper.py`, and `test_split_screen_render.py` all pass under real PostgreSQL/Redis/FFmpeg.

- [ ] **Step 4: Opt-in skip check** — `pytest tests/smoke -q` reports exactly three skips with actionable reasons; `pytest --strict-markers` does not error.

- [ ] **Step 5: License check** — `pytest tests/test_license_stays_open.py -q` PASS; `git ls-files | grep -iE '(^|/)(LICENSE|COPYING)'` is empty.

- [ ] **Step 6: Whole-branch review** — `git log --oneline main..HEAD` (17 commits, one per task), `git diff --check main...HEAD` (no whitespace errors), and a read of `git diff main...HEAD` confirming: no row lock held across a second session; no `mkdir(parents=True)` in `media_storage.py`; every new job dispatch goes through `create_dispatched`; no `file://` in `instagram.py`; no `LICENSE`/`license` added; no credential values committed; no `addopts` smoke filter.

- [ ] **Step 7: Record evidence** — update `docs/VERIFICATION.md` and append a `Verification evidence (<date>): …` line to the `docs/PHASES.md` "V1 completion" section, using the exact command output from Steps 1–6 (test counts, `alembic` messages, the skip list). Write no claim that a command run in this task did not produce.

- [ ] **Step 8: Commit** — `test: verify the v1 completion branch end to end`

---

## Acceptance criteria coverage

| Spec acceptance criterion | Task(s) |
| --- | --- |
| 1. Unknown-kind job fails terminally on real PostgreSQL without hanging; retry refused with 409 | 1 |
| 2. Every rejected media key leaves the outside tree byte-for-byte unchanged | 2 |
| 3. Restored in-flight messages claimed before ready backlog on real Redis | 3 |
| 4. In-flight jobs never exceed `worker_concurrency`; transcription/rendering within stage limits | 4 |
| 5. Two concurrent workers + repeated scheduler passes → exactly one publish job per due publication | 6 |
| 6. Approved clip scheduled manually and automatically, per platform, via authenticated API; IG/YT independent | 7, 10 |
| 7. Failed publication retries within bounded budget through an authenticated endpoint and stops at exhaustion | 6, 10 |
| 8. Editing a scheduled clip returns it to `NEEDS_REVIEW` and cancels its live publications | 8 |
| 9. IG/YT copy stored, edited, validated against platform limits, used in the publish payload | 9, 10 |
| 10. Registering a YouTube channel results in hourly polling that ingests each new video exactly once | 11 |
| 11. Source media older than seven days purged while every generated clip artifact remains | 12 |
| 12. Clip renders with an operator-set focus point and optional deterministic split screen; exact argv + FFprobe | 13 |
| 13. Dashboard supports preview, edit, template, copy, approve, reject, bulk actions, scheduling against the API | 14 |
| 14. Instagram publishing on a default install fails naming `OPENCLIPS_PUBLIC_MEDIA_BASE_URL`; never sends `file://` | 5, 10 |
| 15. The three smoke suites skip cleanly with actionable reasons when their variables are absent | 15 |
| 16. `scripts/verify.sh`, migrations, Ruff, strict mypy pass, and the repository still declares no license | 16, 17 |

## Final review gate

- [ ] Every spec acceptance criterion has current command evidence recorded in `docs/VERIFICATION.md`.
- [ ] `./scripts/verify.sh` is green on the final commit.
- [ ] `git log --oneline main..HEAD` shows one reviewable commit per task, using the exact subjects from each task's final step and no attribution trailer.
- [ ] `git diff --check main...HEAD` is clean.
- [ ] No `LICENSE` file, no `license` field, no committed credential, no `git push`/merge/deploy.
- [ ] Push `fix/v1-completion` and open a pull request only after user review.

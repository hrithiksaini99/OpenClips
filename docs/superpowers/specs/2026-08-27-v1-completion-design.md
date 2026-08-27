# OpenClips V1 Completion Design

## Goal

Close the remaining gap between the verified operational core and the V1 product defined in
`docs/PRD.md`. After this subproject, a self-hosted operator can register a YouTube video, a
YouTube channel, or a local upload; watch clips render automatically with real speaker-focus
framing; review, edit, and approve them in a usable dashboard; write Instagram and YouTube copy;
schedule each platform independently; and have approved clips dispatched, published, and retried
by the running worker — with expired source media cleaned up and generated clips retained.

This design has two distinct halves and they must not be conflated:

1. **Immediate release blockers.** Three confirmed defects in already-shipped code that can hang a
   worker, mutate the filesystem outside the media root, or silently reorder recovered work. These
   are small, isolated, and must be fixed and gated before any feature expansion.
2. **V1 completion work.** Phase 6 was marked `verified` on component tests alone; its scheduling
   and publication behavior has no API surface, no production caller, and no atomic dispatch. Four
   further PRD requirements (channel polling, retention, real concurrency, real framing) are
   likewise unimplemented or only stubbed.

## Scope

In scope: worker deadlock repair, media-root containment, queue recovery ordering, real bounded
worker concurrency, the publication lifecycle and its atomic dispatcher, per-platform scheduling
rules and API, platform-specific post copy, a functional review dashboard, YouTube channel
registration and polling, source retention cleanup, real focus-crop and split-screen rendering, an
Instagram public-media-URL provider abstraction, opt-in real-provider smoke gates, and truthful
documentation.

Out of scope and explicitly deferred: automatic active-speaker detection, analytics, autonomous
publishing, multi-tenancy, additional platforms, non-YouTube ingestion sources, multi-host worker
leases, and any web framework rewrite. The project license stays undecided and must not be invented.

## Principles preserved

- **Local-AI first.** Nothing added here requires a paid API. Post copy is composed deterministically
  from the clip's own transcript; framing is computed arithmetically from configured focus points.
- **Self-hosted first.** New capabilities are configured through `OPENCLIPS_*` environment variables
  and the existing Docker Compose stack. No hosted backend is introduced.
- **Human-controlled automation.** Approval remains mandatory before scheduling. The scheduler
  dispatches only publications an operator explicitly created.
- **Modular providers.** Instagram's public-URL requirement is solved behind a provider interface,
  not by leaking deployment knowledge into the publishing coordinator.
- **PostgreSQL authoritative, Redis at-least-once.** Every new dispatch path goes through the
  existing transactional outbox.

---

# Part 1 — Immediate release blockers

## 1.1 Worker self-deadlock on an unknown job kind

### Defect

`worker._process_payload` opens session A and calls `JobRepository.get_for_update(payload)`, which
issues `SELECT ... FOR UPDATE` and holds a row lock for the life of session A's transaction. When
the claimed job's kind has no registered handler, the function calls `_fail_job(session_factory,
...)`, which opens a **second** session B and issues an `UPDATE` against the same `jobs` row.

On PostgreSQL, session B blocks on session A's row lock. Session A cannot release the lock because
it is synchronously waiting for `_fail_job` to return. PostgreSQL's deadlock detector never fires,
because session A is not waiting on a database lock — it is blocked in Python. The worker thread
hangs indefinitely, the claimed receipt is never acknowledged, and no further jobs are processed.

The exception path is *not* affected: it calls `session.rollback()` before `_fail_job`, releasing
the lock first. Only the unknown-kind branch is broken, and only against a real locking database —
which is why the existing suite (SQLite/in-memory for that path) never caught it.

### Fix

Fail the job in the session that already holds the lock. The unknown-kind branch performs
`START` then `FAIL` on session A and commits, exactly as the exception branch does after its
rollback. `_fail_job` keeps its second-session form only for the post-rollback path, where no lock
is held.

### Legacy-job recovery policy

A job whose kind this build does not register is almost always a row persisted by an older
deployment. The policy is deliberately terminal and non-looping:

- The job is transitioned to `FAILED` with the deterministic reason `UnknownJobKindError: <kind>`,
  so an operator can find every affected row with `GET /api/v1/jobs?job_status=FAILED`.
- The queue receipt is acknowledged. The outbox event was already `DELIVERED`, so the message is
  never redelivered and the worker cannot enter a hot failure loop.
- `application/pipeline.py` gains a canonical `KNOWN_JOB_KINDS` registry and
  `is_known_job_kind(kind)`. The worker's handler map is asserted to cover exactly that registry at
  startup, so a handler-registration mistake fails loudly instead of quarantining live work.
- `POST /api/v1/jobs/{job_id}/retry` returns HTTP 409 naming the unregistered kind when the job's
  kind is not in the registry. Without this guard, retrying a legacy job re-enqueues work that can
  only fail again, forever.
- Startup recovery (`recover_running`) is unchanged: a legacy job left `RUNNING` is recovered to
  `QUEUED`, redelivered once, and then terminally failed by the rule above.

### Required regression

A real-PostgreSQL integration test, not a fake. It seeds a `QUEUED` job with kind
`legacy_unregistered_kind`, runs `_process_payload` with an empty handler map, and asserts the job
reaches `FAILED` with the exact reason. Two safeguards make a regression fail fast instead of
hanging the gate: the engine sets `lock_timeout=3000`, so a blocked second session errors within
three seconds, and the call runs inside a `ThreadPoolExecutor` future with a 30-second
`result(timeout=...)`, so a pure Python hang fails the test.

## 1.2 `MediaStorage.write_stream` mutates the filesystem outside the media root

### Defect

`write_stream` calls `target.parent.mkdir(parents=True, exist_ok=True)` *before* walking the
intermediate components to check for escaping symlinks. If `media_root/a` is a symlink pointing
outside the root and the key is `a/b/c.mp4`, `mkdir(parents=True)` follows the symlink and creates
`<outside>/b` on disk. Only afterwards does the loop detect the escape and raise
`UnsafeMediaPathError`. The rejection is correct; the side effect is not.

`promote_file` already validates before creating, but it then calls `mkdir(parents=True)` too, so
both methods depend on ordering that is easy to reintroduce.

### Fix

A single private `_prepare_target(key) -> Path` becomes the only way either method materializes a
destination directory, and it never uses `parents=True`:

1. Validate the key with the existing `_validate_key`.
2. Ensure the media root exists and resolve it once.
3. Walk intermediate components one at a time. For each component: if the path already exists,
   reject it when `is_symlink()` and its resolution is not relative to the resolved root; otherwise
   create exactly that one directory with `mkdir(exist_ok=True)` and re-check it. Descend only after
   the component is proven contained.
4. Reject a final target that already exists as an escaping symlink, then return the target path.

Because every component is checked before the next one is created, no directory can be created
through a symlink, and rejection happens before any mutation.

`MediaStorage.delete(key) -> bool` (needed by retention, Part 2) reuses the same containment walk
and refuses to unlink through an escaping symlink.

### Required regression

The invariant is **zero filesystem mutation outside `media_root`**. The test creates an outside
directory, snapshots its full recursive contents, symlinks `media_root/escape` to it, calls
`write_stream("escape/nested/payload.bin", ...)`, asserts `UnsafeMediaPathError`, and asserts the
outside snapshot is byte-for-byte unchanged — including that no `nested` directory was created. The
same assertions are repeated for `promote_file` and `delete`, and for a deep key
(`escape/a/b/c.bin`) that would previously have created several directories.

## 1.3 Redis `restore_processing` loses recovered FIFO priority

### Defect

`RedisJobQueue.restore_processing` issues `LMOVE <processing> <ready> LEFT RIGHT`, taking from the
head of the processing list and **appending** to the tail of the ready list. Recovered messages are
older than everything already in `ready`, but they land behind that backlog. After a crash with a
deep queue, work that was already in flight is processed last. `InMemoryJobQueue` has the identical
bug (`popleft` into `append`), so the in-memory tests agree with the wrong behavior.

### Fix

Restore from the tail of processing onto the head of ready: `LMOVE <processing> <ready> RIGHT LEFT`
in a loop. Because `claim` appends to processing with `dest="RIGHT"`, the processing list is
oldest-first; draining it newest-first onto the ready head reverses twice and yields correct
overall order. `InMemoryJobQueue.restore_processing` mirrors this with
`self._ready[q].appendleft(self._processing[q].pop())`.

### Required regression

Both queue implementations get the same contract test: enqueue and claim `job-1` and `job-2`, then
enqueue backlog `job-3`, then `restore_processing`. The subsequent claim order must be exactly
`job-1, job-2, job-3`. The Redis version runs in `tests/integration/test_redis_dispatch.py` against
the reserved logical database so the fix is proven on the real command semantics, not only on the
deque model.

---

# Part 2 — V1 completion

## 2.1 Bounded worker concurrency

`Settings.worker_concurrency` is currently only written to a log line; the worker runs one job at a
time and the setting controls nothing. The PRD requires configurable concurrency for transcription,
analysis, rendering, and publishing, and requires that queued work never launch unlimited
subprocesses.

**Global concurrency.** The worker owns a `ThreadPoolExecutor(max_workers=worker_concurrency)` and a
`BoundedSemaphore(worker_concurrency)`. The claim loop acquires a permit *before* claiming from
Redis and releases it when the job's database outcome has committed and its receipt is acknowledged.
In-flight jobs therefore never exceed `worker_concurrency`, and because each handler runs at most
one external process at a time, concurrent subprocesses are bounded by the same number. Every job
gets its own session from the existing factory, which is already the per-job pattern; sessions are
never shared across threads.

**Per-stage tuning, deliberately minimal.** Transcription and rendering are the CPU-heavy stages and
the ones that will exhaust an 8 GB host first. `application/concurrency.py` provides a `StageLimiter`
holding one named semaphore per limited stage, acquired inside the handler and released in a
`finally`. Exactly two stages are limited in V1 — `transcribe` and `render_clip` — via
`OPENCLIPS_MAX_CONCURRENT_TRANSCRIPTIONS` and `OPENCLIPS_MAX_CONCURRENT_RENDERS`, both defaulting to
`1`. No other per-stage knob is introduced. `Settings` validates that each stage limit is less than
or equal to `worker_concurrency`, because a stage limit above the global limit is meaningless and
signals a misconfiguration.

Shutdown drains the pool: on `KeyboardInterrupt` the worker stops claiming, waits for in-flight
futures, and only then exits. Anything still unacknowledged is recovered by the existing startup
restore.

## 2.2 Publication lifecycle and atomic due dispatch

### Lifecycle

`PublicationStatus` gains `QUEUED` and `CANCELLED`; `PublicationEvent` gains `ENQUEUE` and `CANCEL`.
The transition table becomes:

```text
SCHEDULED  --ENQUEUE--> QUEUED
QUEUED     --START----> PUBLISHING
PUBLISHING --SUCCEED--> PUBLISHED
PUBLISHING --FAIL-----> FAILED
FAILED     --RETRY----> SCHEDULED
SCHEDULED  --CANCEL---> CANCELLED
QUEUED     --CANCEL---> CANCELLED
FAILED     --CANCEL---> CANCELLED
```

`SCHEDULED --START--> PUBLISHING` is removed. `QUEUED` is the state that makes dispatch idempotent:
it records that a job already exists for this publication.

The column is `sa.String(length=32)` with no check constraint, and the longest new value is nine
characters, so no column alteration is required.

### Atomic, idempotent dispatch

`PublicationRepository.claim_due(now, limit)` replaces the read-only `due()` for the dispatch path.
It selects `status == SCHEDULED AND scheduled_at <= now`, ordered by `scheduled_at`, with
`with_for_update(skip_locked=True)`. `ScheduleCoordinator.enqueue_due()` then, inside the same
transaction, transitions each claimed record with `ENQUEUE` and creates its job plus outbox event via
`create_dispatched`. The transaction commits once.

This yields the three required properties:

- **Repeated scheduler passes.** A second pass finds no `SCHEDULED` rows for those publications
  because they are now `QUEUED`. Exactly one job exists per publication per dispatch cycle.
- **Concurrent workers.** `SKIP LOCKED` means a second worker's `claim_due` never sees rows another
  worker has claimed; it takes a disjoint set or none.
- **Duplicate Redis delivery.** Unchanged and already correct: only a `QUEUED` *job* is executed, so
  a redelivered message is acknowledged and ignored.

`publish_publication` now requires `QUEUED` before `START`. A record found `CANCELLED` returns
unchanged without contacting the platform, which is what makes cancellation safe against a job that
is already in flight.

Failure is unchanged in shape and now actually reachable in production: `PUBLISHING -> FAILED`,
then `reschedule_after_failure` applies `RETRY` back to `SCHEDULED` with a backed-off
`scheduled_at`, and the next scheduler pass picks it up. The five-attempt budget and capped
exponential backoff are untouched. An exhausted publication stays `FAILED` and refuses further
retries.

A `ix_publication_records_due` index on `(status, scheduled_at)` supports the claim query.

### Production caller

`application/scheduler.py` provides `PublicationScheduler`, mirroring `OutboxRelay`: it holds a
session factory, the per-platform rules, a clock, and a poll interval, and exposes
`dispatch_once() -> int`. The worker's main loop calls it at most once every
`OPENCLIPS_SCHEDULE_POLL_INTERVAL_SECONDS` (default 30) alongside `relay.dispatch_once()`. This is
the missing production caller for `ScheduleCoordinator.enqueue_due`.

### Cancellation on edit

The PRD says editing a scheduled clip returns it to `NEEDS_REVIEW`. Today that leaves a live
`SCHEDULED` publication that will still be dispatched and published. Every clip edit path
(`PATCH /clips/{id}`, caption edits, framing, template) now calls
`ScheduleCoordinator.cancel_for_clip(clip_id)` in the same transaction as the `EDIT` transition,
cancelling every `SCHEDULED` or `QUEUED` publication for that clip. Already-`PUBLISHED` records are
untouched — history is never rewritten.

## 2.3 Independent per-platform scheduling

`DailyWindowRule` exists but is constructed nowhere. Two settings supply the rules:

```text
OPENCLIPS_INSTAGRAM_SCHEDULE_TIMES=13:00,18:30
OPENCLIPS_YOUTUBE_SCHEDULE_TIMES=16:00
```

`build_services` parses each into a `DailyWindowRule` and exposes
`PlatformScheduleRules: dict[Platform, DailyWindowRule]`. Malformed values raise at startup with the
offending string, so a typo is not silently ignored.

**Automatic queue scheduling** is the default and is a real queue, not a stack: a schedule request
with no explicit timestamp asks the rule for the first slot strictly after
`max(now, latest_scheduled_at(platform))`, so successive approvals lay out across successive slots
rather than colliding on one. `PublicationRepository.latest_scheduled_at(platform)` supplies the
watermark. The computation is deterministic and unit-tested against a frozen clock.

**Manual scheduling** supplies `scheduled_at` explicitly and bypasses the rule. Naive timestamps are
interpreted as UTC, matching the existing coordinator.

Because rules, watermarks, and publication records are all keyed by `Platform`, Instagram and
YouTube schedules are fully independent, which is the PRD requirement.

## 2.4 Platform-specific post copy

A dedicated `clip_platform_copy` table keeps platform knowledge out of the `clips` schema:
`(id, clip_id, platform, title, description, created_at, updated_at)` with a unique
`(clip_id, platform)` index. `ClipCopyRepository` provides `upsert`, `get`, and `list_for_clip`.

`domain/copy.py` holds the pure rules: `INSTAGRAM_CAPTION_LIMIT = 2200`,
`YOUTUBE_TITLE_LIMIT = 100`, `YOUTUBE_DESCRIPTION_LIMIT = 5000`, and
`validate_copy(platform, title, description)` raising `CopyTooLongError` with the platform, field,
actual length, and limit. Instagram's single caption field is modeled as title plus description
joined with a blank line, and the joined length is what is validated.

`application/copy.py` provides a deterministic `CopyComposer.compose(clip, document)` that seeds
default copy at selection time from the clip title and the first sentences of the clip's own
transcript window, truncated on a word boundary to each platform's limit. No model call, no network,
no paid API — consistent with local-AI-first. Operators edit the result; edits are the source of
truth thereafter.

`ScheduleCoordinator._request_for` builds `PublishRequest` from the stored copy for that platform,
falling back to `clip.title` and an empty description when no row exists, so publishing never
depends on the composer having run.

## 2.5 Instagram public media URL provider

`InstagramReelsPublisher` currently sends `video_url = f"file://{path}"` to the Meta graph API. Meta
cannot fetch a `file://` URL; a default local-only install would send a guaranteed-invalid request
and burn its retry budget on it.

`providers/media_urls.py` introduces the abstraction:

- `PublicMediaUnavailableError(PublishError)` — a publish failure with an actionable message.
- `PublicMediaUrlProvider` protocol with `resolve(clip_id: UUID) -> str`.
- `BaseUrlMediaUrlProvider(base_url)` — validates an `http`/`https` scheme and a non-empty host at
  construction and returns `{base_url}/api/v1/clips/{clip_id}/media`.
- `UnavailableMediaUrlProvider` — `resolve` always raises `PublicMediaUnavailableError` naming
  `OPENCLIPS_PUBLIC_MEDIA_BASE_URL` and explaining that Instagram must fetch the clip over a
  publicly reachable URL.
- `build_media_url_provider(base_url)` returns the unavailable provider when the setting is empty,
  which is the default.

`PublishRequest` gains `media_url: str | None = None`. `InstagramReelsPublisher` sends
`request.media_url` and raises `PublishError` when it is `None`; it never constructs a URL itself and
never emits `file://`. `YouTubeShortsPublisher` is untouched — it uploads bytes and needs no public
URL.

Failure is surfaced at the earliest boundary rather than at publish time: scheduling a clip on
`INSTAGRAM_REELS` while `public_media_base_url` is unset returns HTTP 409 with the same actionable
message. The runtime guard remains as defense in depth.

`GET /api/v1/clips/{clip_id}/media` streams the rendered artifact with `FileResponse`, resolved
through `MediaStorage` so containment is enforced, and 404s when the clip has no render. It sits on
the existing public read surface, consistent with the already-public catalog and review endpoints;
this is a deliberate, documented choice for a single-workspace self-hosted product, and it is what
makes an operator-supplied reverse-proxy origin sufficient for Instagram. `GET
/api/v1/clips/{clip_id}/caption` serves the caption artifact the same way.

## 2.6 Scheduling and publication API

All mutations require the existing admin bearer token; reads stay public, matching Phase 5.

```text
POST   /api/v1/clips/{clip_id}/schedule      {platform, scheduled_at?}   -> PublicationOut
POST   /api/v1/clips/bulk-schedule           {clip_ids, platform, scheduled_at?} -> [BulkPublicationResultItem]
GET    /api/v1/publications                  ?platform=&publication_status=&limit=
GET    /api/v1/publications/{publication_id}
POST   /api/v1/publications/{publication_id}/retry
POST   /api/v1/publications/{publication_id}/cancel
PUT    /api/v1/clips/{clip_id}/copy/{platform}  {title, description} -> ClipCopyOut
GET    /api/v1/clips/{clip_id}/copy             -> [ClipCopyOut]
```

Documented status codes: 404 for unknown clip or publication; 409 for a clip that is not `APPROVED`
(`ClipNotApprovedError`), for an exhausted retry budget (`SchedulingExhaustedError`), for an invalid
lifecycle transition, and for Instagram scheduling without a configured public media base URL; 422
for a copy body exceeding a platform limit. Bulk scheduling returns one result item per clip with
`ok` and either the publication id or the error, mirroring the existing `/clips/bulk` shape.

These endpoints live in `api/publishing_routes.py`, a separate `APIRouter` included by
`build_router`, so the growing surface stays legible and task file ownership stays clean.

## 2.7 YouTube channel registration and polling

A `youtube_channels` table stores `(id, channel_url, external_id, poll_interval_seconds, enabled,
auto_process, last_polled_at, last_error, created_at, updated_at)` with a unique `external_id`.

`providers/youtube.py` gains `extract_youtube_channel_id(url)` accepting `/channel/UC…`, `/@handle`,
`/c/<name>`, and `/user/<name>` forms and rejecting everything else with the existing
`UnsupportedMediaLocator`; `canonicalize_youtube_channel_url(url)`; and `YtDlpChannelLister`, which
builds shell-free argv (`yt-dlp --flat-playlist --print id -I 1:N -- <url>`) with a bounded timeout
and a truncated stderr tail, exactly like `YtDlpDownloader`.

`application/channels.py` provides:

- `ChannelIngestionCoordinator.register(url, *, auto_process, poll_interval_seconds)` — canonicalizes,
  reuses an existing channel by `external_id`, and returns the record. Registration never downloads.
- `ChannelPoller.poll_due(now) -> int` — for each enabled channel where
  `domain/channels.is_due(last_polled_at, poll_interval_seconds, now)`, list up to
  `OPENCLIPS_CHANNEL_POLL_MAX_VIDEOS` recent video ids and register each new one through the existing
  `YouTubeIngestionCoordinator` path.

**Per-video idempotency** reuses the proven mechanism: each video's source key is
`youtube_idempotency_key(video_id)`, and `source_assets.idempotency_key` is unique. A video already
registered is skipped without creating a second source or a second ingest job; a concurrent insert
that loses the race raises `IntegrityError`, which is caught per video and treated as "already
registered". `last_polled_at` advances on every completed poll — including one that finds nothing —
so a failing channel cannot spin. A lister failure records `last_error` on the channel, advances
`last_polled_at`, and does not abort the remaining channels.

The worker calls `poll_due` on its loop, rate-limited by the channel's own interval; the default
interval is one hour, satisfying "polled hourly by default with a configurable interval".

API (`api/channel_routes.py`): `POST /api/v1/channels`, `GET /api/v1/channels`,
`PATCH /api/v1/channels/{channel_id}` (enable/disable, interval, `auto_process`), and
`POST /api/v1/channels/{channel_id}/poll` for an immediate operator-triggered poll.

## 2.8 Source retention

The PRD requires source videos retained 7 days by default and generated clips retained forever.
`SOURCE_RETENTION_DAYS = 7` and `source_assets.retain_until` already exist and are already populated;
nothing ever acts on them.

`source_assets` gains `media_purged_at`. `domain/retention.py` holds the pure predicate
`is_purgeable(*, status, retain_until, media_path, media_purged_at, now)`.
`application/retention.py` provides `RetentionSweeper.sweep(now) -> int`, which for each candidate:

1. Skips the source when any job with that source in its payload is `QUEUED` or `RUNNING`.
2. Skips the source when any of its clips still lacks `output_path` and is not `REJECTED` — a source
   is never purged out from under work that still needs it.
3. Deletes the source media through `MediaStorage.delete(media_path)`, sets `media_purged_at`, and
   clears `media_path`.

Clip `output_path` and `caption_path` are never touched, and no clip row is deleted. The source row
itself is retained as history. The worker runs the sweeper every
`OPENCLIPS_RETENTION_SWEEP_INTERVAL_SECONDS` (default 3600).

A purged source that an operator later wants to reprocess is re-ingestable: the YouTube path already
resumes an incomplete source through its idempotency key.

## 2.9 Real framing: focus crop and split screen

### Honest V1 boundary

**OpenClips V1 does not perform automatic active-speaker detection.** Nothing in the codebase
analyzes audio or video to locate a speaker, and this subproject does not add it. `SpeakerCropStrategy`
currently returns an empty filter tuple, so the "speaker-aware crop" claim in the Phase 4 record is
an abstraction, not a behavior. The documentation is corrected to say so plainly.

What V1 delivers instead is **manual, metadata-driven framing**: the operator sets a normalized
horizontal focus point per clip (or two, for split screen) in the review dashboard, and the renderer
honors it exactly and deterministically. This is genuinely useful for podcast layouts, is fully
testable, and does not overstate the product.

### Implementation

`SpeakerCropStrategy` and `CenterCropStrategy` are replaced by `FocusCropStrategy(focus_x)`, which
computes a real crop window:

- Target aspect is `width:height` (9:16). Crop height is the source height and crop width is
  `round(source_height * width / height)`; if that exceeds the source width, crop width becomes the
  source width and crop height is `round(source_width * height / width)`.
- Both dimensions are rounded down to even values, because libx264 requires even dimensions.
- `x = clamp(round(focus_x * source_width - crop_width / 2), 0, source_width - crop_width)` and
  `y = clamp((source_height - crop_height) // 2, 0, source_height - crop_height)`.
- The emitted filter is `crop=<cw>:<ch>:<x>:<y>`.

At `focus_x = 0.5` this is arithmetically equivalent to today's scale-to-fill-then-center-crop
output, so existing renders are unchanged by default.

`SplitScreenLayout(top_focus_x, bottom_focus_x)` produces the optional deterministic two-speaker
layout. Each half is `width x (height // 2)` at aspect `width:(height//2)`, cropped around its own
focus point by the same arithmetic, and stacked:

```text
[0:v]crop=cw:ch:x1:y1,scale=W:H2,setsar=1[top];
[0:v]crop=cw:ch:x2:y2,scale=W:H2,setsar=1[bot];
[top][bot]vstack=inputs=2[v]
```

`RenderLayout = SingleCropLayout | SplitScreenLayout`. `build_render_argv` emits `-vf` for the
single-crop layout (unchanged shape) and `-filter_complex` with `-map "[v]" -map "0:a?"` for split
screen. Subtitles are appended to the final filter node in both cases, so caption behavior is
identical across layouts.

`clips` gains `layout` (`SINGLE` or `SPLIT`, default `SINGLE`), `focus_x` (default `0.5`),
`split_top_focus_x`, and `split_bottom_focus_x`. All four are editable through `PATCH /clips/{id}`,
validated to `[0.0, 1.0]`, and an edit moves the clip to `NEEDS_REVIEW` and cancels any live
publication. `RenderCoordinator` builds the layout from the clip row rather than from a
service-level singleton, so per-clip framing actually reaches FFmpeg.

Argv construction is asserted exactly in unit tests for several source geometries, and one real
FFmpeg integration test renders a split-screen clip and asserts 1080x1920 output via FFprobe.

## 2.10 Functional review dashboard

The current dashboard is a three-column read-only HTML table built by string concatenation. The PRD
requires preview, start/end adjustment, subtitle editing, template selection, Instagram and YouTube
copy editing, approval, rejection, bulk actions, and — now — scheduling.

The dashboard stays a server-rendered page plus vanilla JavaScript against the documented JSON API.
No framework, no build step, no SPA. `jinja2` is the only new runtime dependency; templates live at
`src/openclips/api/templates/dashboard.html` and static assets at
`src/openclips/api/static/dashboard.{js,css}`, shipped inside the installed package and resolved
relative to `__file__`.

Capabilities:

- **Queue view** — clips filtered by status, with title, status, score, and duration.
- **Preview** — `<video>` against `GET /api/v1/clips/{id}/media`, plus rendered metadata (dimensions,
  template, timespan, caption path) from `GET /api/v1/clips/{id}`.
- **Edit** — title, start/end, caption template (validated against the six built-in templates),
  subtitle word edits, and framing (layout, focus points).
- **Copy** — per-platform title and description editors for Instagram and YouTube, showing the
  remaining character budget for each platform limit.
- **Review** — approve and reject, with 409 conflicts surfaced verbatim to the operator.
- **Bulk** — multi-select with bulk approve, bulk reject, and bulk schedule.
- **Schedule** — platform selector, optional date/time (empty means automatic queue slot), and a
  publications panel showing status, scheduled time, attempts, error, and external URL, with retry
  and cancel actions.

Authentication in the browser: the operator pastes the configured admin token once into a field on
the page; it is held in `sessionStorage` and sent as `Authorization: Bearer …` on every mutation.
This introduces no cookie, no session store, and no new credential. Reads work without it.

Server-side tests assert the page renders, references its static assets, and exposes the review
queue. Behavior is verified through the JSON endpoints the page calls, which are contract-tested
directly; no browser automation dependency is added.

## 2.11 Opt-in real-provider smoke gates

Three real-dependency behaviors cannot run in the default gate without network access or secrets,
and none may become mandatory:

| Suite | File | Enabled by | Proves |
| --- | --- | --- | --- |
| Real transcription | `tests/smoke/test_real_whisper.py` | `OPENCLIPS_SMOKE_REAL_WHISPER=1` | faster-whisper downloads, loads, and returns normalized word timings for a generated audio fixture |
| Live YouTube | `tests/smoke/test_live_youtube.py` | `OPENCLIPS_SMOKE_YOUTUBE_URL` and `OPENCLIPS_SMOKE_YOUTUBE_CHANNEL_URL` | a real video downloads and promotes; a real channel lists video ids idempotently |
| Sandbox publishing | `tests/smoke/test_sandbox_publish.py` | `OPENCLIPS_SMOKE_SANDBOX_PUBLISH=1` plus the operator's own already-configured platform credentials | an adapter reaches a real sandbox endpoint and preserves its response |

Each module declares `pytestmark = [pytest.mark.smoke, pytest.mark.skipif(...)]` and skips with an
actionable reason when its variable is absent. The `smoke` marker is registered in `pyproject.toml`
so `--strict-markers` passes. No `addopts` filter is added, so the default gate simply reports them
as skipped and `scripts/verify.sh` stays hermetic. `scripts/smoke.sh` documents how an operator runs
them with their own credentials. No credential value is ever committed, and CI configuration is not
given secrets.

## 2.12 Truthful documentation

`progress.md` is created at the repository root as the single status surface, recording each phase,
what is actually verified, and what is explicitly deferred — including the statement that automatic
active-speaker detection is not implemented. `docs/PHASES.md` gains a "V1 completion" section and its
Phase 6 entry is corrected to state that its original gate covered components in isolation and that
the scheduling API, production dispatcher, and atomic due-claim landed here. `docs/VERIFICATION.md`
records only evidence produced by commands actually run. `README.md` documents the new settings,
endpoints, dashboard, channel polling, retention, framing, and the opt-in smoke script.

No claim about live faster-whisper, live YouTube, or real platform publishing is written unless the
corresponding smoke suite was actually executed and its output recorded. The repository adds no
`LICENSE` file and no `license` field in `pyproject.toml`; a test asserts both, so the open decision
in the PRD stays open.

---

## Configuration added

```text
OPENCLIPS_MAX_CONCURRENT_TRANSCRIPTIONS=1
OPENCLIPS_MAX_CONCURRENT_RENDERS=1
OPENCLIPS_SCHEDULE_POLL_INTERVAL_SECONDS=30
OPENCLIPS_CHANNEL_POLL_INTERVAL_SECONDS=3600
OPENCLIPS_CHANNEL_POLL_MAX_VIDEOS=10
OPENCLIPS_RETENTION_SWEEP_INTERVAL_SECONDS=3600
OPENCLIPS_INSTAGRAM_SCHEDULE_TIMES=13:00,18:30
OPENCLIPS_YOUTUBE_SCHEDULE_TIMES=16:00
OPENCLIPS_PUBLIC_MEDIA_BASE_URL=
```

`OPENCLIPS_PUBLIC_MEDIA_BASE_URL` is empty by default: a local-only install must fail clearly rather
than attempt Instagram publishing.

## Migrations added

| Revision | Adds |
| --- | --- |
| `0009_publication_dispatch` | `ix_publication_records_due` on `(status, scheduled_at)` |
| `0010_clip_platform_copy` | `clip_platform_copy` table with unique `(clip_id, platform)` |
| `0011_youtube_channels` | `youtube_channels` table with unique `external_id` |
| `0012_source_retention` | `source_assets.media_purged_at` |
| `0013_clip_framing` | `clips.layout`, `clips.focus_x`, `clips.split_top_focus_x`, `clips.split_bottom_focus_x` |

Each follows the existing conventions: an explicit revision id, a defensive `downgrade` that tolerates
missing objects, and a verified `upgrade head` / `check` / `downgrade -1` / `upgrade head` round trip
against a disposable database.

## Error and consistency rules

- No worker code path holds a database row lock across a second session that touches the same row.
- No media operation mutates the filesystem outside `media_root`, including on the rejection path.
- Recovered in-flight messages are always processed before ready backlog.
- In-flight jobs never exceed `worker_concurrency`; transcription and rendering never exceed their
  stage limits; no code path spawns an unbounded number of subprocesses.
- A publication is dispatched at most once per scheduling cycle regardless of pass count or worker
  count.
- Editing a clip cancels its live publications in the same transaction as the lifecycle transition.
- Instagram publishing never emits a `file://` URL; an unconfigured deployment fails with a message
  naming the setting to configure.
- Channel polling registers each video at most once, and a channel that errors does not block others.
- Retention deletes source media only; generated clips and captions are never deleted.
- Every new dispatch goes through the transactional outbox; Redis remains at-least-once only.

## Verification strategy

Unit tests cover the pure rules: known-job-kind registry, media-key containment, queue ordering,
stage limiter behavior, publication transitions, slot computation, copy limits, framing arithmetic
and exact argv, channel due-computation, and retention predicates.

Integration tests against real PostgreSQL and Redis cover the unknown-kind regression with a lock
timeout, restore ordering on real Redis list commands, concurrent `claim_due` from two sessions,
concurrent bounded worker execution, channel polling idempotency across repeated passes, and a
real-FFmpeg split-screen render.

`scripts/verify.sh` remains the documented full gate and continues to use a disposable
`openclips_test_*` database and the reserved Redis logical database 15, never touching the developer
database or named volumes. `scripts/smoke.sh` is the separate, opt-in, operator-run gate for real
providers.

## Acceptance criteria

The subproject is complete only when all of the following are verified on the final tree:

1. A claimed job with an unregistered kind fails terminally on real PostgreSQL without hanging, and
   retrying it is refused with an actionable 409.
2. Every rejected media key leaves the filesystem outside `media_root` byte-for-byte unchanged.
3. Restored in-flight messages are claimed before pre-existing ready backlog on real Redis.
4. Concurrent in-flight jobs never exceed `worker_concurrency`, and transcription and rendering never
   exceed their stage limits.
5. Two concurrent workers and repeated scheduler passes produce exactly one publish job per due
   publication.
6. An approved clip can be scheduled manually and automatically, per platform, through authenticated
   API calls, and Instagram and YouTube schedules are independent.
7. A failed publication retries within its bounded budget through an authenticated endpoint and stops
   at exhaustion.
8. Editing a scheduled clip returns it to `NEEDS_REVIEW` and cancels its live publications.
9. Instagram and YouTube copy is stored, edited, validated against platform limits, and used in the
   publish payload.
10. Registering a YouTube channel results in hourly polling that ingests each new video exactly once.
11. Source media older than seven days is purged while every generated clip artifact remains present.
12. A clip renders with an operator-set focus point and, optionally, a deterministic two-speaker
    split screen, proven by exact argv and a real FFprobe assertion.
13. The dashboard supports preview, edit, template, copy, approve, reject, bulk actions, and
    scheduling against the documented API.
14. Instagram publishing on a default local install fails with a message naming
    `OPENCLIPS_PUBLIC_MEDIA_BASE_URL` and never sends `file://`.
15. The three smoke suites skip cleanly with actionable reasons when their variables are absent.
16. `scripts/verify.sh`, migrations, Ruff, and strict mypy pass, and the repository still declares no
    license.

## Lifecycle commands

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec api alembic upgrade head
./scripts/verify.sh          # hermetic full gate
./scripts/smoke.sh           # opt-in real-provider checks, operator credentials only
docker compose down          # retains database, media, and model volumes
```

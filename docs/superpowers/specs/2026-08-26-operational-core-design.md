# OpenClips Operational Core Design

## Goal

Make the documented Docker Compose path operate as one durable workflow: an authenticated
creator uploads a local MP4/MOV file or registers one YouTube video, OpenClips processes it
through ingestion, local transcription, clip selection, and rendering, and the resulting clips
reach `READY_FOR_REVIEW` without manual database or Redis intervention.

This design repairs the integration gaps between existing, tested components. It preserves the
modular-monolith architecture and the existing domain/provider boundaries.

## Scope

The operational core includes:

- persistent media and model-cache volumes shared by the required services;
- Docker environment propagation and faster-whisper installation;
- authenticated local-upload and single-video YouTube ingestion endpoints;
- durable PostgreSQL-to-Redis job dispatch through a transactional outbox;
- hybrid pipeline orchestration: automatic by default with manual stage control retained;
- worker-start recovery, failed-job retry, and duplicate-message suppression;
- an end-to-end Docker verification gate using PostgreSQL, Redis, FFmpeg, and deterministic
  transcription data.

The following remain outside this subproject: YouTube channel polling, multi-host worker leases,
speaker tracking, split-screen rendering, the review-dashboard redesign, social scheduling, and
real platform publishing.

## Architectural decisions

PostgreSQL remains the source of truth for domain data, jobs, and dispatch intent. Redis remains a
delivery mechanism rather than the authoritative job store. API handlers and worker stages create
a job record and its outbox event in the same database transaction. The existing worker process
runs both an outbox-relay cycle and queue-consumer cycle; no additional dispatcher service is
introduced.

Delivery is at least once. If the relay crashes after pushing a job ID to Redis but before marking
the outbox row delivered, it may push the same ID again. The worker atomically transitions only a
`QUEUED` job to `RUNNING`; messages for jobs in any other state are acknowledged and ignored.
Queue claims atomically move messages to a per-queue processing list and remove them only after the
database transaction finishes. Stage handlers remain idempotent so a deliberately retried job can
safely replace its own output.

API and worker containers share `/data/media`. The worker stores faster-whisper models in a
persistent Hugging Face cache volume. The model downloads automatically on first transcription;
download and load failures are recorded on the transcription job.

## Components

### Outbox record and repository

An `outbox_events` table stores:

- UUID primary key;
- indexed job ID;
- Redis queue name;
- status (`PENDING` or `DELIVERED`);
- attempt count;
- next-attempt timestamp;
- last error;
- created and delivered timestamps.

`JobRepository.create_dispatched(...)` creates the job and outbox row through one application
boundary. Manual retries transition a failed job back to `QUEUED` and create a fresh pending
outbox event. Every dispatch cycle has its own outbox UUID, so a job can retain multiple historical
events across explicit retries.

### Outbox relay

The relay claims a bounded batch of due pending rows with PostgreSQL row locking, pushes each job
ID to its queue, and marks successful deliveries. Redis failures increment attempts, preserve a
truncated actionable error, and set a capped exponential retry time. API requests do not fail
after their database transaction commits merely because Redis is temporarily unavailable.

The Redis queue implementation uses a reliable claim: claiming atomically moves a message from
the ready list to a processing list, and acknowledgement removes it after the job transaction
commits. Worker startup restores unacknowledged processing messages to their ready queues. A crash
after job commit but before acknowledgement therefore produces only a harmless duplicate.

### Ingestion API

`POST /api/v1/sources/upload` accepts a multipart MP4/MOV upload and an `auto_process` flag that
defaults to `true`. It streams the request to safe media storage without buffering the entire file,
enforces a configurable byte limit, and returns `202 Accepted` with the source and nullable next
job. Identical file bytes reuse the existing source. When `auto_process=false`, the response has no
next job.

`POST /api/v1/sources/youtube` accepts a supported single-video URL and `auto_process`, also
defaulting to `true`. It canonicalizes the URL, derives the video identity, registers or reuses the
source, and creates an `ingest_youtube` job. The API returns `202 Accepted` before the download.
The worker downloads into a temporary file under the shared media volume and atomically promotes
the completed file. Duplicate canonical video URLs reuse the existing source.

Both endpoints require the existing admin bearer token. Shorts, embed, live, playlist, and channel
URLs remain rejected by this endpoint.

### Pipeline orchestration

Sources persist whether automatic processing is enabled. A successful automatic stage creates its
successor job and outbox event in the same transaction as its output:

```text
local upload -------------------> transcribe
YouTube URL -> ingest_youtube --> transcribe
transcribe ---------------------> select_clips
select_clips -------------------> render_clip (one job per candidate)
render_clip --------------------> READY_FOR_REVIEW artifact
```

Selection that produces zero candidates succeeds without creating render jobs. Render jobs are
independent, so one failed clip does not block its siblings. With `auto_process=false`, ingestion
stops after the source becomes ready. Existing manual transcribe, selection, and render endpoints
create jobs through the same outbox boundary.

### Worker recovery and retry

The V1 recovery boundary is one Compose worker service. Before beginning its normal loop, the
worker restores Redis processing messages, returns jobs left `RUNNING` by its previous process to
`QUEUED`, and creates new outbox events. Duplicate messages created by the two recovery paths are
suppressed by the atomic job-status claim. Multi-host leases and heartbeats are deferred.

`POST /api/v1/jobs/{job_id}/retry` requires admin authentication, accepts only `FAILED` jobs,
transitions the job to `QUEUED`, and creates an outbox event. Provider, model, download, and FFmpeg
failures remain visible on the job. Temporary download artifacts are removed on failure.

## Configuration and Compose

Compose mounts:

- `media-data:/data/media` into API and worker;
- `model-cache:/root/.cache/huggingface` into worker and read-only into API;
- `.env` into API and worker configuration, while internal PostgreSQL and Redis service URLs remain
  explicit Compose overrides.

The runtime image installs the transcription dependency. `.dockerignore` excludes Git metadata,
virtual environments, caches, local media, linked worktrees, and brainstorming artifacts.

New configuration includes a maximum upload size and the relay batch/backoff settings. Existing
shell-free process execution, timeouts, safe filenames, and stderr truncation remain mandatory.

`GET /ready` continues to represent API dependencies and therefore checks PostgreSQL and Redis.
`GET /api/v1/system/transcription-readiness` inspects the shared cache and a download marker to
report `missing`, `downloading`, or `available`; a missing model does not make the API unavailable
because first-use download is intentional.

## Error and consistency rules

- A committed API job cannot be lost because Redis is down.
- A worker crash after a Redis claim cannot lose the claimed message.
- Duplicate Redis messages cannot execute a non-queued job.
- Source and job idempotency conflicts return the existing resource rather than creating duplicate
  media or work.
- Database failures roll back both the job and dispatch intent.
- Filesystem writes use temporary paths and atomic promotion; incomplete files are not exposed as
  ready media.
- Failed automatic stages stop their branch and preserve their reason. They never silently skip to
  a later stage.
- Explicit retries use the same durable dispatch path as initial work.

## Verification strategy

Unit tests cover outbox creation, relay retry/backoff, duplicate suppression, automatic/manual
stage decisions, upload validation, YouTube canonicalization, model-readiness states, and job retry
validation.

Integration tests use disposable PostgreSQL and real Redis to prove job/outbox atomicity, relay
delivery, worker consumption, worker-start recovery, and API/worker access to shared media.

The Docker end-to-end gate generates a tiny MP4 with FFmpeg, uploads it through the authenticated
API, uses deterministic injected transcript data, and waits for the automatic chain to create
reviewable 1080x1920 rendered clips. Real faster-whisper model download and live YouTube download
remain opt-in smoke tests because they require network access and materially increase gate time.

`scripts/verify.sh` is the documented full gate. It builds the images, uses an isolated test
database, runs the full pytest suite, Ruff, mypy, migration checks, the API health smoke test, and
the Docker pipeline test without destroying the developer database.

## Acceptance criteria

The subproject is complete only when all of the following are verified on the final tree:

1. A fresh clone can build and start API, worker, PostgreSQL, and Redis with documented commands.
2. API and worker see the same persistent source and generated media.
3. A local upload with default automation reaches rendered `READY_FOR_REVIEW` clips.
4. A supported YouTube URL returns promptly and its background ingestion is retryable.
5. Redis unavailability after API acceptance does not lose the job.
6. Duplicate delivery does not execute a job twice.
7. A worker restart recovers work left `RUNNING` by its prior process.
8. The first real transcription can download and persist the configured model automatically.
9. Manual stage dispatch and failed-job retry use the same outbox mechanism.
10. `scripts/verify.sh`, migrations, Ruff, and strict mypy pass.

## Lifecycle commands

Start or update the stack:

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec api alembic upgrade head
```

Run the complete gate:

```bash
./scripts/verify.sh
```

Stop services while retaining PostgreSQL, media, and model data:

```bash
docker compose down
```

Removing named volumes is intentionally not part of the normal lifecycle because it permanently
deletes the database, media, and downloaded models.

# OpenClips Product Requirements Document

## 1. Product definition

OpenClips is a free, open-source, self-hosted platform that turns long-form podcast, interview, and talking-head videos into short-form vertical clips and distributes approved clips to social platforms.

Users can provide a YouTube video URL, a YouTube channel URL, or a local video upload. OpenClips ingests the media, transcribes it locally, identifies coherent moments, creates 9:16 clips with speaker-aware framing and animated subtitles, generates platform-specific post copy, and places results in a human review queue. Approved clips can be scheduled independently for Instagram Reels and YouTube Shorts.

The V1 core workflow must be possible without paid AI APIs. Users control their infrastructure, models, credentials, source files, and generated files.

## 2. Vision and principles

### V1 vision

> Turn a long-form podcast into publishable short-form content with minimal manual work.

### Long-term vision

> Become an autonomous, open-source AI social-media manager that starts with video.

V1 is deliberately limited to the foundational content-repurposing workflow. Analytics, trend discovery, autonomous publishing, and broad video-category support are future work.

Core principles:

- Self-hosted first: no proprietary hosted backend is required.
- Local-AI first: media and transcripts can remain on user-controlled infrastructure.
- Human-controlled automation: publication requires explicit approval in V1.
- Creator-owned files: generated media is ordinary filesystem-accessible output.
- API first: the web interface consumes documented backend capabilities.
- Modular providers: sources, transcription, analysis, storage, rendering, and platforms have replaceable interfaces.

## 3. Target users and success

The primary user is a podcast creator or small content team that publishes long-form episodes and wants to reduce manual clipping, editing, captioning, and cross-platform posting. Technical creators, agencies, and self-hosting enthusiasts are secondary users.

The first usable release succeeds when a user can deploy with Docker Compose, supply one video, receive multiple coherent clips in a configured output directory, review and edit them, approve selected clips, and see them scheduled or published to Instagram Reels and YouTube Shorts.

## 4. V1 scope

### Supported inputs

- One YouTube video URL, processed once.
- One YouTube channel URL, polled hourly by default with a configurable interval.
- Local MP4 or MOV upload, subject to FFmpeg codec support.

### Supported content

V1 is optimized for podcasts, interviews, and conversational talking-head content. Gaming, vlogs, sports, tutorials, lectures, and arbitrary compositions are future scope.

### Processing pipeline

```text
source -> ingestion -> audio extraction -> local transcription
       -> word timestamps -> transcript segmentation
       -> local analysis -> boundary/context refinement
       -> speaker-aware reframing -> subtitles -> vertical render
       -> platform-specific post copy -> human review
       -> approval -> platform queues -> publishing
```

### Clip behavior

- Default maximum: up to 10 clips per source video.
- Configurable maximum: 3–30 clips.
- Target duration: 20–90 seconds; preferred 30–60 seconds.
- Do not manufacture clips to hit the maximum.
- Prefer self-contained moments; extend boundaries when context is required.
- Remove obvious boundary dead air without aggressive jump-cut editing.
- No mandatory viral score or performance prediction in V1.

### Video and subtitles

- Primary output aspect ratio: 9:16.
- Attempt active-speaker crop/reframing for podcast layouts.
- Provide a simple optional two-speaker split-screen layout.
- Store editable word-level subtitle timing as structured data.
- Provide 4–6 built-in templates such as Minimal, Bold, Karaoke, Podcast, High Contrast, and Clean.
- Support word-level highlighting or equivalent animated progression.
- Preserve profanity by default; provide optional masking at workspace level.
- Support transcript correction before final rendering.

### Review and approval

Each generated clip must support preview, start/end adjustment, transcript/subtitle editing, caption-template selection, Instagram copy editing, YouTube copy editing, approval, and rejection. Bulk approve/reject actions are required.

Primary clip lifecycle:

```text
GENERATING -> READY_FOR_REVIEW -> APPROVED -> SCHEDULED -> PUBLISHED
                         |             |            |
                         v             v            v
                      REJECTED      NEEDS_REVIEW  FAILED
```

Editing an approved or scheduled clip returns it to `NEEDS_REVIEW`. Publishing failures are retryable and preserve an explicit failure reason.

### Scheduling and publishing

- Independent queues and posting rules per platform.
- Automatic queue scheduling is the default, with manual date/time assignment available.
- Different posting times are supported per platform.
- V1 platforms: Instagram Reels and YouTube Shorts.
- Publication status records platform, timestamp, external post ID, URL when available, and failure details.
- Human approval is always required before a clip enters a publishing queue.

## 5. Deployment and operations

Target installation:

```bash
git clone <repository>
cd OpenClips
cp .env.example .env
docker compose up -d
```

The deployable product consists of a web application/API, background worker, PostgreSQL, queue, local media storage, transcription/AI runtime, and FFmpeg-capable processing runtime. The exact service split may evolve while preserving one-product deployment.

Minimum target is approximately 4 CPU cores, 8 GB RAM, and CPU-only operation. Recommended target is 8+ CPU cores, 16+ GB RAM, and optional GPU acceleration. GPU is never mandatory.

Expensive work is queued with configurable concurrency for transcription, analysis, rendering, and publishing. Queued work must not launch unlimited subprocesses or exhaust the host.

Source videos are retained for 7 days by default. Generated clips are retained forever by default. V1 is a single workspace/single organization with one admin account and multiple connected social accounts.

## 6. Architecture requirements

Recommended building blocks are FastAPI, PostgreSQL, a Redis-compatible queue, SQLAlchemy/Alembic, FFmpeg, faster-whisper or equivalent, and an Ollama-compatible local model runtime. These are replaceable choices; the product requirements are the boundaries and behavior.

Required boundaries:

- Domain models and state transitions are independent of HTTP and provider SDKs.
- Application services orchestrate resumable and idempotent workflows.
- Provider adapters isolate YouTube, Instagram, YouTube publishing, transcription, LLM analysis, and storage.
- API contracts are documented and versioned.
- Background jobs record lifecycle state, attempts, timestamps, and errors.

## 7. Explicit non-goals for V1

- General-purpose video editing, full trend analysis, autonomous publishing, analytics, performance optimization, multi-tenancy, plugin marketplace, and mandatory paid AI services.
- TikTok, X, LinkedIn, Facebook, S3/Drive/RSS ingestion, and broad video-category support.

## 8. Open decisions

- Project license remains undecided and must not be invented in repository metadata.
- Exact local model defaults and GPU backends will be selected during implementation.
- Final web UI framework is not fixed; API-first behavior takes priority.

## 9. Quality bar

Every phase must have explicit acceptance criteria, automated tests, and a recorded verification gate. Dependent phases may start only after the gate passes. Independent workstreams may run in parallel only when file ownership and integration contracts are non-overlapping. The main validator is responsible for inspecting all changes and running the complete relevant suite.

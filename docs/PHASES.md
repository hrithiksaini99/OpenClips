# OpenClips phased implementation plan

Status uses `planned`, `in progress`, `blocked`, and `verified`.

## Phase 0 — Architecture and repository foundation

Status: verified

Depends on: none.

Deliverables: repository structure, architecture docs, environment configuration, Docker Compose development stack, FastAPI application shell, PostgreSQL migration boundary, core source/job/clip lifecycle model, resumable state-transition service, health/readiness endpoints, test harness, CI configuration, and contributor documentation.

Acceptance criteria:

1. A fresh checkout can start the API, worker, database, and queue with Docker Compose.
2. The API exposes health and dependency readiness checks.
3. Invalid lifecycle transitions are rejected deterministically.
4. Job state and clip state are persisted behind an application boundary.
5. Tests cover domain transitions, persistence integration, and API health behavior.
6. Static checks and the full test suite run through one documented command.

Verification gate: the main validator reviews the diff, runs unit tests, integration tests, lint/type checks, and a Compose smoke test. Only then can dependent phases begin.

Verification evidence (2026-08-26): Docker Compose rebuilt successfully; PostgreSQL migration upgraded successfully; `alembic check` reported no new operations; container suite passed 14 tests; Ruff and mypy passed; `/health` returned `{"status":"ok"}`; `/ready` returned healthy database and Redis checks. Phase 1 may now begin.

## Phase 1 — Media ingestion

Status: verified

Depends on: Phase 0 verified.

Deliverables: local upload and YouTube video adapters, source metadata, safe filenames, download progress, retention metadata, duplicate/idempotency handling, and ingestion job recovery.

Acceptance criteria: supported local files and YouTube URLs produce registered source assets; malformed inputs fail clearly; retries do not duplicate assets; source retention metadata is recorded.

Verification gate: fixture-based adapter tests plus a local FFmpeg/media smoke test.

Verification evidence (2026-08-26): 84 tests passed in the Compose API container with PostgreSQL, including adapter contract tests, idempotent replay of a real FFmpeg-generated MP4, FFprobe container validation, and failed-source recovery; Ruff and mypy passed; `alembic downgrade base && alembic upgrade head` cycled cleanly and `alembic check` reported no new operations. Phase 2 may now begin.

## Phase 2 — Local transcription

Status: verified

Depends on: Phase 1 verified.

Deliverables: transcription provider interface, local faster-whisper adapter, normalized transcript/segment/word data, model readiness checks, and resumable transcription jobs.

Acceptance criteria: a fixture produces normalized timestamps; model absence is actionable; retries resume or replace deterministically; no paid API is required.

Verification gate: deterministic fixture tests and an opt-in local-model integration test.

Verification evidence (2026-08-26): 106 tests passed in the Compose API container with PostgreSQL, covering normalized transcript invariants, faster-whisper normalization via injected fake models, Redis-compatible queue contract, and the full enqueue-fail-retry-succeed worker loop; Ruff and mypy passed; `alembic downgrade base && alembic upgrade head` cycled cleanly and `alembic check` reported no new operations. The real-model path is gated behind the `transcription` extra (`faster-whisper`) and exercised through the same contract via fake models; no paid API is used. Phase 3 may now begin.

## Phase 3 — Clip selection and boundary refinement

Status: verified

Depends on: Phase 2 verified.

Deliverables: transcript-first candidate selection, configurable 3–30 clip maximum, 20–90 second boundaries, self-contained context extension, dead-air trimming, and local LLM provider interface.

Acceptance criteria: coherent fixture transcripts produce bounded candidates without forcing clip count; malformed model output is rejected safely; candidates are reproducible from the same input.

Verification gate: golden transcript tests and provider contract tests.

Verification evidence (2026-08-26): 134 tests passed in the Compose API container with PostgreSQL. Golden transcript tests prove bounded non-overlapping candidates, reproducibility across runs, dead-air trimming to word boundaries, minimum-duration growth into neighbor segments, maximum-duration shrinking by edge score, and no forced clip count (short transcripts yield none). Provider contract tests cover the `ClipRefiner` interface, strict-JSON local LLM refiner parsing, nine malformed-output rejection cases, and safe fallback to heuristic candidates. Selection clips persist with timespans, titles, and scores through migration `0004_clip_selection`; migrations cycled up/down/up cleanly with `alembic check` clean; Ruff and mypy passed. Phase 4 may now begin.

## Phase 4 — Rendering and captioning

Status: verified

Depends on: Phase 2 verified; Phase 3 verified for end-to-end rendering.

Deliverables: 9:16 rendering, caption templates, word highlighting, profanity masking, transcript edits, speaker-aware crop abstraction, and generated-file persistence.

Acceptance criteria: fixture clips render playable vertical media; edited words appear in subtitles; built-in templates validate; rendering failure is resumable.

Verification gate: FFprobe assertions, subtitle tests, and sample artifact review.

Verification evidence (2026-08-26): 158 tests passed in the Compose API container with PostgreSQL and real FFmpeg/FFprobe. A real render produced a playable 1080x1920 MP4 from a fixture clip with burned karaoke captions; FFprobe asserted video/audio streams and dimensions, and the caption artifact contained the edited word. All six built-in templates validate; SRT/ASS generation is deterministic with word highlighting (`\kf` tags), transcript edits, and opt-in profanity masking. The renderer builds shell-free argv behind an injectable runner with center/speaker crop strategies. Rendered artifacts persist on clips (migration `0005_rendered_clips`); failed renders are resumable through job retry. Ruff and mypy passed; migrations cycled cleanly with `alembic check` clean. Phase 5 may now begin.

## Phase 5 — Review dashboard and API

Status: planned

Depends on: Phase 0 verified; Phase 4 verified for preview integration.

Deliverables: admin auth, source/job/clip API, review queue, preview metadata, edit operations, bulk actions, and API documentation.

Acceptance criteria: unauthenticated users cannot mutate content; a creator can review/edit/approve/reject one or many clips; edits trigger documented lifecycle behavior.

Verification gate: API contract tests and browser-level smoke tests.

## Phase 6 — Scheduling and platform publishing

Status: planned

Depends on: Phase 5 verified.

Deliverables: platform account abstractions, independent queues, manual and rule-based scheduling, Instagram Reels adapter, YouTube Shorts adapter, retry/backoff, and publication records.

Acceptance criteria: only approved clips schedule; platform schedules are independent; retries are bounded and observable; adapter failures preserve state and reason.

Verification gate: fake-platform contract tests plus opt-in sandbox tests.

## Parallelization policy

After Phase 0 is verified, Phase 1 and provider-contract portions of Phase 5 may proceed independently with strict file ownership. Phase 2 depends on ingestion contracts. Phase 3 depends on normalized transcripts. Phase 4 depends on clip contracts. Phase 6 depends on review and approval semantics. No parallel worker may edit shared migration files, lifecycle enums, API schemas, or central configuration without coordination.

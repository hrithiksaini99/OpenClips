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

## Phase 2 — 2026-08-26

- Local suite: 88 passed, 18 skipped (PostgreSQL-gated).
- Full suite in Compose (API container, PostgreSQL + ffmpeg): 106 passed.
- New domain data: `TranscriptWord`, `TranscriptSegment`, `TranscriptDocument` with normalized second-based timing and invariant validation.
- Provider contract: `TranscriptionProvider` base with `is_ready`/`readiness`/`transcribe`; `FasterWhisperProvider` normalizes raw segments deterministically and reports actionable model absence (`ModelUnavailableError`). Real model load is opt-in via the `transcription` extra.
- Durable coordination: `RedisJobQueue` (BLPOP claim) and `InMemoryJobQueue` share one surface; jobs table gained a `payload` column; `transcripts` table stores the full normalized document as JSON with unique source index (migration `0003_transcripts`).
- Worker is real: `process_once` claims payloads, owns lifecycle transitions, persists failures with attempt counts, and marks unknown job kinds FAILED instead of crashing.
- Resumability proof: scripted provider fails once then succeeds through retry — transcript replaced, job SUCCEEDED, attempts tracked.
- Ruff: passed. mypy: no issues in 24 source files. Migrations cycled up/down/up cleanly; `alembic check` clean.

## Phase 3 — 2026-08-26

- Local suite: 116 passed, 18 skipped (PostgreSQL-gated).
- Full suite in Compose (API container, PostgreSQL + ffmpeg): 134 passed.
- Deterministic engine: hook/rate segment scoring, min-duration growth, max-duration shrink, word-boundary dead-air trim, overlap rejection, count never forced; same input always yields the same candidates (`SelectionBounds` enforces 3–30 clips, ordered durations).
- Refiner contract: `ClipRefiner` base, `HeuristicClipRefiner` baseline, `LocalLlmClipRefiner` strict-JSON adapter; malformed output raises `MalformedModelOutputError` and the coordinator falls back to heuristic candidates safely.
- Persistence: clips gained `source_asset_id`, `title`, `start_time`, `end_time`, `selection_score` (migration `0004_clip_selection`); re-selection replaces previous clips deterministically; worker handles the `select_clips` job kind end-to-end.
- Operational fix: shared dev-database drift from host-run integration tests no longer wedges the gate — downgrades tolerate missing objects and `downgrade base && upgrade head` self-heals.
- Ruff: passed. mypy: no issues in 28 source files. Migrations cycled cleanly; `alembic check` clean.

## Phase 4 — 2026-08-26

- Local suite: 139 passed, 18 skipped (PostgreSQL-gated).
- Full suite in Compose (API container, PostgreSQL + real FFmpeg): 158 passed.
- Sample artifact review: FFmpeg rendered a fixture clip into a 1080x1920 H.264/AAC MP4 with burned karaoke captions; FFprobe asserted streams, dimensions, and container format.
- Captions: six validated built-in templates (minimal, bold, karaoke, podcast, high_contrast, clean); deterministic SRT cues plus ASS with per-word `\kf` highlight timing for word-highlight styles; transcript edits and opt-in profanity masking apply before rendering and appear in subtitles.
- Renderer: shell-free argv construction, injected process runner, center and speaker-aware crop abstractions, ffprobe metadata parsing; failures raise `RenderError` with a truncated stderr tail.
- Persistence: clips record `caption_path`, `caption_template`, `render_width`, `render_height`, `output_path` (migration `0005_rendered_clips`).
- Resumability: render jobs fail durably with error text and retry replaces artifacts deterministically.
- Ruff: passed. mypy: no issues in 32 source files. Migrations cycled cleanly; `alembic check` clean.

## Phase 5 — 2026-08-26

- Local suite: 150 passed, 19 skipped (PostgreSQL-gated).
- Full suite in Compose: 169 passed.
- Auth: fail-closed admin bearer token (`OPENCLIPS_ADMIN_TOKEN`); missing configuration yields 503 on mutations, bad credentials yield 401; reads stay public.
- API surface under `/api/v1`: sources list/detail with transcribe and select-clips dispatch, jobs list/detail with status and kind filters, review queue with status filter and preview metadata (render/caption paths, dimensions), clip detail, PATCH edits (title/timespan) moving clips to NEEDS_REVIEW per the documented lifecycle, caption word-edit persistence, approve/reject transitions with 409 conflicts, bulk actions with per-item results, render dispatch, and an HTML dashboard.
- API documentation served by FastAPI at `/docs`.
- Live smoke checks against Compose: `/docs`, `/api/v1/clips`, `/api/v1/dashboard` returned 200; unauthenticated approve returned 503 without a configured token.
- Operational fix: shared integration fixture (`tests/integration/conftest.py`) drops the alembic stamp after tests so `alembic upgrade head` always rebuilds cleanly.
- Ruff: passed. mypy: no issues in 37 source files.

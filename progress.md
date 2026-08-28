# OpenClips progress

Last verified: 2026-08-28

## Working V1

- Local upload and YouTube video ingestion with resumable persisted job state.
- Local faster-whisper transcription behind a provider abstraction.
- Transcript-first clip selection with deterministic fallback behavior.
- 1080x1920 FFmpeg rendering with six caption templates, word highlighting,
  transcript edits, and optional profanity masking.
- Authenticated review/edit/approve/reject and bulk review operations, plus a
  simple HTML review queue and interactive OpenAPI documentation.
- Independent Instagram Reels and YouTube Shorts publication queues with
  atomic due-item claims, transactional outbox dispatch, bounded retries,
  manual retry/cancel, and edit-triggered cancellation.
- PostgreSQL as the authoritative store, Redis at-least-once delivery, shared
  media/model volumes, bounded worker concurrency, and recovery on restart.

## Final verification

- `./scripts/verify.sh`: 325 passed, 1 expected environment skip.
- Ruff: passed.
- Strict mypy: passed across 51 source files.
- Alembic upgrade/check: passed with no pending operations.
- Host Compose resolution: passed.
- Live stack: `/health`, `/ready`, `/docs`, dashboard, jobs, clips, and
  publications all returned HTTP 200 on `http://localhost:8002`.
- Auth gate: an unauthenticated publication mutation returned 401; an
  authenticated incomplete request reached schema validation and returned 422.

## Running instance

- Worktree: `/Users/hrithik/Desktop/OpenClips/.worktrees/v1-completion`
- Branch: `fix/v1-completion`
- API/docs: `http://localhost:8002` and `http://localhost:8002/docs`
- Dashboard: `http://localhost:8002/api/v1/dashboard`
- Local admin token is stored only in the ignored `.env` file.

## Intentionally deferred

Automatic channel polling, retention sweeps, configured daily posting windows,
platform-specific copy persistence, advanced face/split framing, polished SPA,
and live third-party publishing smoke tests requiring user-owned credentials.
The repository license remains undecided as requested.

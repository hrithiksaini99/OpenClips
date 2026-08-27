# OpenClips

OpenClips is a self-hosted, local-AI-first platform for turning podcast and interview videos into reviewable short-form clips. It runs the whole pipeline durably: PostgreSQL owns jobs and a transactional outbox, the worker relays due events onto reliable Redis lists, and one authenticated upload or YouTube URL flows automatically from ingestion through transcription, selection, and 9:16 rendering.

## Pipeline

1. **Ingest** a local `.mp4`/`.mov` upload (`POST /api/v1/sources/upload`, bounded streaming) or a YouTube watch URL (`POST /api/v1/sources/youtube`, background download via shell-free yt-dlp).
2. **Transcribe** locally with faster-whisper through durable outbox-dispatched jobs (`transcribe`).
3. **Select** deterministic candidates with dead-air trimming and an LLM refiner contract (`select_clips`).
4. **Render** 9:16 media with caption templates, word highlighting, profanity masking, and transcript edits (`render_clip`).
5. **Review** via the admin API and dashboard: edit, approve, reject, bulk actions.
6. **Publish** approved clips to Instagram Reels and YouTube Shorts on independent per-platform queues with bounded backoff.

When a source opts into automation (`auto_process`, the default), each successful stage dispatches its successor in the same database transaction, so the pipeline advances with no manual queue writes. Set `auto_process=false` to drive the stages manually through the existing per-stage endpoints. Model download progress is observable, without gating the pipeline, at `GET /api/v1/system/transcription-readiness`.

## Development

Requirements: Docker with Compose and Python 3.11+ for local tooling.

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec api alembic upgrade head
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

Stop services while retaining database data:

```bash
docker compose down
```

Run local checks with a Python 3.11+ environment:

```bash
pip install -e '.[dev]'
pytest -q
ruff check src tests
mypy src
```

Integration persistence tests run when `DATABASE_URL` points to a disposable PostgreSQL database named `openclips_test_*` (and `REDIS_URL` for the Redis-backed tests); otherwise they skip explicitly. They never touch the developer database or named volumes.

### Full verification gate

`scripts/verify.sh` runs the complete gate in the Compose stack against a disposable database and a reserved Redis logical database, without disturbing the developer database, named volumes, or a running dev `api` service:

```bash
./scripts/verify.sh
```

It applies migrations, runs the full suite (including the real PostgreSQL/Redis/FFmpeg `upload → transcribe → select → render` integration test), Ruff, strict mypy, `alembic check`, and an HTTP `/health` + `/ready` smoke check on a one-off container. Run it with host ports 5432/6379 free (stop any other stack bound to them first).

## Review API

Interactive documentation is served at `http://localhost:8000/docs`. All mutating endpoints require `Authorization: Bearer $OPENCLIPS_ADMIN_TOKEN`; when no token is configured they fail closed with HTTP 503. Reads are public.

## Configuration

Platform credentials stay empty by default; publishing adapters activate only after `OPENCLIPS_INSTAGRAM_ACCOUNT_ID`, `OPENCLIPS_INSTAGRAM_ACCESS_TOKEN`, and `OPENCLIPS_YOUTUBE_ACCESS_TOKEN` are set. Local transcription uses faster-whisper (opt-in extra: `pip install '.[transcription]'`).

See [docs/PRD.md](docs/PRD.md), [docs/PHASES.md](docs/PHASES.md), and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for scope and verification gates.

The project license is intentionally undecided.

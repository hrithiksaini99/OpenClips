# OpenClips

OpenClips is a self-hosted, local-AI-first platform for turning podcast and interview videos into reviewable short-form clips. All six implementation phases are complete: ingestion, local transcription, clip selection, vertical rendering, the review API, and platform publishing.

## Pipeline

1. **Ingest** a local `.mp4`/`.mov` upload or a YouTube watch URL (yt-dlp, shell-free).
2. **Transcribe** locally with faster-whisper through durable queue jobs (`transcribe`).
3. **Select** deterministic 20–90 second candidates with dead-air trimming and an LLM refiner contract (`select_clips`).
4. **Render** 9:16 media with caption templates, word highlighting, profanity masking, and transcript edits (`render_clip`).
5. **Review** via the admin API and dashboard: edit, approve, reject, bulk actions.
6. **Publish** approved clips to Instagram Reels and YouTube Shorts on independent per-platform queues with bounded backoff.

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

Integration persistence tests run when `DATABASE_URL` points to PostgreSQL; otherwise they skip explicitly. Integration tests leave the shared database empty and unstamped, so run `alembic upgrade head` afterwards before using the live API.

## Review API

Interactive documentation is served at `http://localhost:8000/docs`. All mutating endpoints require `Authorization: Bearer $OPENCLIPS_ADMIN_TOKEN`; when no token is configured they fail closed with HTTP 503. Reads are public.

## Configuration

Platform credentials stay empty by default; publishing adapters activate only after `OPENCLIPS_INSTAGRAM_ACCOUNT_ID`, `OPENCLIPS_INSTAGRAM_ACCESS_TOKEN`, and `OPENCLIPS_YOUTUBE_ACCESS_TOKEN` are set. Local transcription uses faster-whisper (opt-in extra: `pip install '.[transcription]'`).

See [docs/PRD.md](docs/PRD.md), [docs/PHASES.md](docs/PHASES.md), and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for scope and verification gates.

The project license is intentionally undecided.

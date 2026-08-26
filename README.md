# OpenClips

OpenClips is a self-hosted, local-AI-first platform for turning podcast and interview videos into reviewable short-form clips. The project is in foundation development; media ingestion and AI processing are intentionally not implemented yet.

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

Integration persistence tests run when `DATABASE_URL` points to PostgreSQL; otherwise they skip explicitly. See [docs/PRD.md](docs/PRD.md), [docs/PHASES.md](docs/PHASES.md), and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for scope and verification gates.

The project license is intentionally undecided.

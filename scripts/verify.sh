#!/usr/bin/env bash
# Isolated operational-core verification gate.
#
# Runs the full suite (including the real PostgreSQL/Redis/FFmpeg integration
# tests), Ruff, strict mypy, and an Alembic migration round-trip against a
# DISPOSABLE database and a RESERVED Redis logical database. It never touches the
# developer database, named media/model volumes, or the long-running dev `api`
# service: the HTTP smoke check runs in a one-off container on a spare port.
set -euo pipefail

VERIFY_DB="openclips_test_$$"
VERIFY_REDIS_DB=15
SMOKE_PORT=8001
VERIFY_URL="postgresql+psycopg://openclips:openclips@db:5432/${VERIFY_DB}"
REDIS_URL="redis://redis:6379/${VERIFY_REDIS_DB}"
SMOKE_NAME="openclips-verify-api-$$"

cleanup() {
  docker rm -f "${SMOKE_NAME}" >/dev/null 2>&1 || true
  docker compose exec -T redis redis-cli -n "${VERIFY_REDIS_DB}" FLUSHDB >/dev/null 2>&1 || true
  docker compose exec -T db dropdb --force --if-exists -U openclips "${VERIFY_DB}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> Starting disposable infrastructure (db, redis) and building images"
docker compose up -d db redis
docker compose build api worker

echo "==> Provisioning disposable database ${VERIFY_DB} and reserved Redis db ${VERIFY_REDIS_DB}"
docker compose exec -T redis redis-cli -n "${VERIFY_REDIS_DB}" FLUSHDB >/dev/null
docker compose exec -T db createdb -U openclips "${VERIFY_DB}"

echo "==> Applying migrations to the disposable database"
docker compose run --rm --no-deps \
  -e OPENCLIPS_DATABASE_URL="${VERIFY_URL}" \
  api alembic upgrade head
docker compose run --rm --no-deps \
  -e OPENCLIPS_DATABASE_URL="${VERIFY_URL}" \
  api alembic check

echo "==> Running the full test suite with real PostgreSQL and Redis"
docker compose run --rm --no-deps \
  -e DATABASE_URL="${VERIFY_URL}" \
  -e REDIS_URL="${REDIS_URL}" \
  api pytest -q

echo "==> Ruff and strict mypy"
docker compose run --rm --no-deps api ruff check src tests
docker compose run --rm --no-deps api mypy src

echo "==> HTTP smoke check in a one-off container on port ${SMOKE_PORT}"
# --publish remaps to a spare host port so the long-running dev `api` (if any)
# on 8000 is never disturbed; --no-deps reuses the db/redis already started.
docker compose run --rm --no-deps -d \
  --name "${SMOKE_NAME}" \
  --publish "${SMOKE_PORT}:8000" \
  -e OPENCLIPS_DATABASE_URL="${VERIFY_URL}" \
  -e OPENCLIPS_REDIS_URL="${REDIS_URL}" \
  api >/dev/null

for _ in $(seq 1 30); do
  if curl --fail --silent "http://localhost:${SMOKE_PORT}/health" >/dev/null; then
    break
  fi
  sleep 1
done
curl --fail --silent "http://localhost:${SMOKE_PORT}/health" >/dev/null
curl --fail --silent "http://localhost:${SMOKE_PORT}/ready" >/dev/null

echo "==> Verification succeeded"

#!/usr/bin/env bash
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL is required}"
: "${POSTGRES_HOST:=db}"
: "${POSTGRES_PORT:=5432}"
: "${API_HOST:=0.0.0.0}"
: "${API_PORT:=8000}"

echo "Waiting for PostgreSQL at ${POSTGRES_HOST}:${POSTGRES_PORT}..."
until pg_isready -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" >/dev/null 2>&1; do
  sleep 2
done

echo "Applying database patches..."
PSQL=psql DATABASE_URL="${DATABASE_URL}" /app/db/apply-local.sh

echo "Seeding workflow test users from environment..."
DATABASE_URL="${DATABASE_URL}" PYTHONPATH=/app/api python /app/api/scripts/seed_login_users.py

echo "Seeding Derisk360 Group operational data..."
DATABASE_URL="${DATABASE_URL}" PYTHONPATH=/app/api python /app/api/scripts/seed_derisk360_data.py || echo "Operational data seed skipped or failed"

echo "Syncing Derisk360 employees from organization directory..."
DATABASE_URL="${DATABASE_URL}" PYTHONPATH=/app/api python /app/api/scripts/sync_employees.py || echo "Employee sync skipped or failed — check MS Graph settings in infra/.env"

echo "Starting SLMCT API..."
if [[ "${DEVTOOLS_HOT_RELOAD:-}" == "true" || "${APP_ENV:-}" == "development" ]]; then
  exec uvicorn app.main:app --host "${API_HOST}" --port "${API_PORT}" --reload --reload-dir /app/api
fi
exec uvicorn app.main:app --host "${API_HOST}" --port "${API_PORT}"

#!/usr/bin/env sh
set -euo pipefail

echo "Running database migrations..."
alembic upgrade head

# Render and other PaaS set PORT; keep API_PORT for Docker Compose local overrides.
LISTEN_PORT="${PORT:-${API_PORT:-8000}}"
echo "Starting API on port ${LISTEN_PORT}..."
exec uvicorn app.main:app --host "${API_HOST:-0.0.0.0}" --port "${LISTEN_PORT}"

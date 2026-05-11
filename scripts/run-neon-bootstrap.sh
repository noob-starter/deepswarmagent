#!/usr/bin/env bash
# Apply scripts/neon-bootstrap-schema.sql to Neon (or any Postgres) using psql.
#
# Usage:
#   export DATABASE_URL='postgresql://user:pass@ep-xxx.neon.tech/neondb?sslmode=require'
#   ./scripts/run-neon-bootstrap.sh
#
# DATABASE_URL may use SQLAlchemy async form — this script strips the driver for psql.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SQL_FILE="${ROOT}/scripts/neon-bootstrap-schema.sql"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is not set." >&2
  exit 1
fi

RAW="${DATABASE_URL}"
if [[ "${RAW}" == postgresql+psycopg_async://* ]]; then
  RAW="postgresql://${RAW#postgresql+psycopg_async://}"
elif [[ "${RAW}" == postgresql+asyncpg://* ]]; then
  RAW="postgresql://${RAW#postgresql+asyncpg://}"
fi
export PGSSLMODE="${PGSSLMODE:-require}"

echo "Applying ${SQL_FILE} ..."
psql "${RAW}" -v ON_ERROR_STOP=1 -f "${SQL_FILE}"
echo "Done."

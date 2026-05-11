# syntax=docker/dockerfile:1
# Build context must be the repository root (same as backend/Dockerfile).
# Some hosts only look for ./Dockerfile; this file mirrors the API image.

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/pyproject.toml backend/alembic.ini ./
COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/scripts/docker-entrypoint.sh /docker-entrypoint.sh

RUN pip install --no-cache-dir .

RUN chmod +x /docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/docker-entrypoint.sh"]

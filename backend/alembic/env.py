"""Alembic migration environment — uses *sync* Postgres URLs derived from ``DATABASE_URL``."""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.db import models  # noqa: F401 — register ORM metadata
from app.db.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_sync_url() -> str:
    """
    Same normalized ``DATABASE_URL`` as the API (quote strip, channel_binding strip,
    ``postgresql+asyncpg`` → ``postgresql`` for sync Alembic/psycopg2), plus
    ``sslmode=require`` when missing for Neon/Supabase hosts.
    """
    url = get_settings().database_url
    if url.startswith("postgresql+asyncpg://"):
        sync = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    else:
        sync = url
    low = sync.lower()
    if (
        ("neon.tech" in low or "supabase.co" in low or "supabase.com" in low)
        and "sslmode=" not in low
        and "ssl=" not in low
    ):
        sync = f"{sync}{'&' if '?' in sync else '?'}sslmode=require"
    return sync


def run_migrations_offline() -> None:
    """Generate SQL scripts without a live DB connection."""
    url = get_sync_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations inside a sync connection (Alembic default)."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_sync_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

"""Alembic migration environment — uses *sync* Postgres URLs derived from ``DATABASE_URL``."""

from __future__ import annotations

import logging
import sys
from logging.config import fileConfig
from pathlib import Path
from urllib.parse import urlparse

from alembic import context
from sqlalchemy import create_engine, pool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.db import models  # noqa: F401 — register ORM metadata
from app.db.base import Base
from app.db.pg_network import (
    finalize_libpq_url,
    is_supabase_direct_hostname,
    resolve_supabase_direct_ipv4,
)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
logger = logging.getLogger("alembic.env")


def get_sync_url() -> str:
    """Normalized sync URL for Alembic offline mode / display (includes hostaddr in URI when enabled)."""
    s = get_settings()
    return finalize_libpq_url(
        s.database_url,
        use_supabase_ipv4=s.database_supabase_ipv4,
        explicit_hostaddr=s.database_hostaddr,
        embed_hostaddr_in_query=True,
    )


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
    s = get_settings()
    # psycopg2 via SQLAlchemy often ignores hostaddr if it is only in the URL query string.
    url = finalize_libpq_url(
        s.database_url,
        use_supabase_ipv4=False,
        explicit_hostaddr=None,
        embed_hostaddr_in_query=False,
    )
    connect_args: dict[str, str] = {}
    if s.database_supabase_ipv4:
        parsed = urlparse(url)
        host = parsed.hostname
        if is_supabase_direct_hostname(host):
            ip = resolve_supabase_direct_ipv4(host or "", explicit=s.database_hostaddr)
            if ip:
                connect_args["hostaddr"] = ip
                logger.info("Alembic connect using hostaddr=%s (hostname=%s)", ip, host)
            else:
                logger.warning(
                    "No IPv4 resolved for %s — migrations will likely fail on IPv4-only networks. "
                    "Set DATABASE_HOSTADDR or use Supabase Session pooler.",
                    host,
                )

    connectable = create_engine(
        url,
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

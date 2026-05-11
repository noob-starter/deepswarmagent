"""
Async SQLAlchemy engine and session factory.

A single PostgreSQL instance backs sessions, audit snapshots, and any future
tables—no Redis required for this backend slice.
"""

import ssl
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.base import Base

_settings = get_settings()


def _asyncpg_connect_args(database_url: str) -> dict[str, Any]:
    """
    Cloud Postgres (Neon, Supabase, etc.) expects TLS; asyncpg does not infer
    ``sslmode=require`` from the URI unless we pass ``ssl=`` explicitly.

    Supabase’s transaction pooler (PgBouncer) requires disabling asyncpg’s
    prepared statement cache (``statement_cache_size=0``).
    """
    parsed = urlparse(database_url)
    host = (parsed.hostname or "").lower()
    port = parsed.port
    lower = database_url.lower()

    needs_ssl = (
        "neon.tech" in lower
        or "supabase.co" in host
        or "supabase.com" in host
        or "sslmode=require" in lower
        or "sslmode%3drequire" in lower  # URL-encoded =
        or "ssl=true" in lower
    )
    pooler_like = (
        "pooler.supabase.com" in host
        or port == 6543
        or "pgbouncer=true" in lower
    )

    args: dict[str, Any] = {}
    if needs_ssl:
        args["ssl"] = ssl.create_default_context()
    if pooler_like:
        args["statement_cache_size"] = 0
    return args


engine = create_async_engine(
    _settings.database_url,
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_max_overflow,
    pool_pre_ping=True,
    connect_args=_asyncpg_connect_args(_settings.database_url),
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: one session per request."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create tables if they do not exist (MVP); prefer Alembic in production."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

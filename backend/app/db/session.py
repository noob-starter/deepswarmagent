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
from app.db.pg_network import asyncpg_url_replace_host_with_ipv4

_settings = get_settings()


def _asyncpg_connect_args(
    canonical_database_url: str,
    *,
    ssl_relaxed_hostname: bool,
) -> dict[str, Any]:
    """
    Hosted Postgres (e.g. Supabase) expects TLS; asyncpg does not infer
    ``sslmode=require`` from the URI unless we pass ``ssl=`` explicitly.

    PgBouncer transaction pool (Supabase port 6543) needs
    ``statement_cache_size=0``.

    When connecting by IPv4 to a hostname-issued cert, TLS hostname check must be
    relaxed while still verifying the certificate chain (``CERT_REQUIRED``).
    """
    parsed = urlparse(canonical_database_url)
    host = (parsed.hostname or "").lower()
    port = parsed.port
    lower = canonical_database_url.lower()

    needs_ssl = (
        "supabase.co" in host
        or "supabase.com" in host
        or "pooler.supabase.com" in host
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
        if ssl_relaxed_hostname:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_REQUIRED
            args["ssl"] = ctx
        else:
            args["ssl"] = ssl.create_default_context()
    if pooler_like:
        args["statement_cache_size"] = 0
    return args


_effective_url = _settings.database_url
_ssl_relaxed = False
if _settings.database_supabase_ipv4:
    _effective_url, _ssl_relaxed = asyncpg_url_replace_host_with_ipv4(_settings.database_url)

engine = create_async_engine(
    _effective_url,
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_max_overflow,
    pool_pre_ping=True,
    connect_args=_asyncpg_connect_args(
        _settings.database_url,
        ssl_relaxed_hostname=_ssl_relaxed,
    ),
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

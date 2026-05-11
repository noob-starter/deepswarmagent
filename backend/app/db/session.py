"""
Async SQLAlchemy engine and session factory.

Uses **psycopg3 async** (``postgresql+psycopg_async``, libpq) — the same connection
stack family as Alembic (psycopg2). This avoids asyncpg-vs-libpq differences (IPv6
preference, TLS/SNI) that break Supabase pooler on Render while migrations succeed.
"""

import logging
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.base import Base
from app.db.pg_network import (
    asyncpg_url_replace_host_with_ipv4,
    is_supabase_direct_hostname,
    rewrite_supabase_direct_to_session_pooler_any,
)

_settings = get_settings()
logger = logging.getLogger(__name__)


def _psycopg_async_connect_args(url: str) -> dict[str, Any]:
    """
    PgBouncer **transaction** pool needs prepared statements off in psycopg.
    Session pool on 5432 keeps defaults.
    """
    parsed = urlparse(url)
    port = parsed.port or 5432
    lower = url.lower()
    if port == 6543 or "pgbouncer=true" in lower or "pool_mode=transaction" in lower:
        return {"prepare_threshold": None}
    return {}


_effective_url = _settings.database_url

if _settings.database_supabase_ipv4:
    u2, _ = asyncpg_url_replace_host_with_ipv4(
        _effective_url,
        explicit_ipv4=_settings.database_hostaddr,
    )
    _effective_url = u2
    if _effective_url == _settings.database_url and is_supabase_direct_hostname(
        urlparse(_settings.database_url).hostname
    ):
        alt = rewrite_supabase_direct_to_session_pooler_any(
            _settings.database_url,
            region_hint=_settings.supabase_pooler_region,
        )
        if alt:
            _effective_url = alt

_p = urlparse(_effective_url)
logger.info(
    "SQLAlchemy async engine (psycopg_async) host=%s port=%s",
    _p.hostname,
    _p.port or 5432,
)

engine = create_async_engine(
    _effective_url,
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_max_overflow,
    pool_pre_ping=True,
    connect_args=_psycopg_async_connect_args(_effective_url),
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

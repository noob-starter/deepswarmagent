"""
Async SQLAlchemy engine and session factory.

A single PostgreSQL instance backs sessions, audit snapshots, and any future
tables—no Redis required for this backend slice.
"""

import ssl
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.base import Base

_settings = get_settings()

_NEON = "neon.tech" in _settings.database_url.lower()


def _asyncpg_connect_args(database_url: str) -> dict[str, Any]:
    """Neon requires TLS; asyncpg does not infer sslmode= from the URI alone."""
    if _NEON:
        return {"ssl": ssl.create_default_context()}
    return {}


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

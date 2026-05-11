"""
Async SQLAlchemy engine and session factory.

A single PostgreSQL instance backs sessions, audit snapshots, and any future
tables—no Redis required for this backend slice.
"""

import logging
import ssl
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.base import Base
from app.db.pg_network import (
    asyncpg_pooler_url_prefer_ipv4_literal,
    asyncpg_url_replace_host_with_ipv4,
    is_supabase_direct_hostname,
    rewrite_supabase_direct_to_session_pooler_any,
)

_settings = get_settings()
logger = logging.getLogger(__name__)


def _asyncpg_connect_args(
    canonical_database_url: str,
    *,
    ssl_relaxed_hostname: bool,
) -> dict[str, Any]:
    """
    Hosted Postgres (e.g. Supabase) expects TLS; asyncpg does not infer
    ``sslmode=require`` from the URI unless we pass ``ssl=`` explicitly.

    Supabase **transaction** pool (port ``6543``, or ``pgbouncer=true``) needs
    ``statement_cache_size=0``. Session pooler on port ``5432`` should keep the default
    prepared-statement cache.

    When connecting by IPv4 literal (hostname rewritten), ``*supabase*`` is no longer
    in the host segment — ``ssl_relaxed_hostname`` forces TLS on.

    When connecting by IPv4 to a hostname-issued cert, TLS hostname check must be
    relaxed while still verifying the certificate chain (``CERT_REQUIRED``).
    """
    parsed = urlparse(canonical_database_url)
    host = (parsed.hostname or "").lower()
    port = parsed.port or 5432
    lower = canonical_database_url.lower()

    needs_ssl = (
        ssl_relaxed_hostname
        or "supabase.co" in host
        or "supabase.com" in host
        or "pooler.supabase.com" in host
        or "sslmode=require" in lower
        or "sslmode%3drequire" in lower  # URL-encoded =
        or "ssl=true" in lower
    )
    transaction_pooler = (
        port == 6543
        or "pgbouncer=true" in lower
        or "pool_mode=transaction" in lower
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
    if transaction_pooler:
        args["statement_cache_size"] = 0
    if needs_ssl:
        args["timeout"] = 60.0
    return args


_effective_url = _settings.database_url
_ssl_relaxed = False

# Supabase session pooler: asyncpg may try IPv6 first; Render is IPv4-only — prefer literal IPv4.
_lit, _rel = asyncpg_pooler_url_prefer_ipv4_literal(_effective_url)
_effective_url, _ssl_relaxed = _lit, _rel

if _settings.database_supabase_ipv4:
    u2, r2 = asyncpg_url_replace_host_with_ipv4(
        _effective_url,
        explicit_ipv4=_settings.database_hostaddr,
    )
    _effective_url = u2
    _ssl_relaxed = _ssl_relaxed or r2
    if _effective_url == _settings.database_url and is_supabase_direct_hostname(
        urlparse(_settings.database_url).hostname
    ):
        alt = rewrite_supabase_direct_to_session_pooler_any(
            _settings.database_url,
            region_hint=_settings.supabase_pooler_region,
        )
        if alt:
            lit_alt, rel_alt = asyncpg_pooler_url_prefer_ipv4_literal(alt)
            _effective_url = lit_alt
            _ssl_relaxed = _ssl_relaxed or rel_alt

_p = urlparse(_effective_url)
logger.info(
    "SQLAlchemy async engine using host=%s port=%s (ssl_relaxed_hostname=%s)",
    _p.hostname,
    _p.port or 5432,
    _ssl_relaxed,
)

engine = create_async_engine(
    _effective_url,
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_max_overflow,
    pool_pre_ping=True,
    connect_args=_asyncpg_connect_args(
        _effective_url,
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

"""
Helpers for hosted Postgres (Supabase direct connection on db.*.supabase.co).

Supabase’s direct host often resolves to IPv6 first; many PaaS networks (e.g. Render)
are IPv4-only and fail with "Network is unreachable". We fix that by:

- **libpq** (Alembic/psycopg2, LangGraph): append ``hostaddr=<IPv4>`` while keeping
  the real hostname for TLS and auth.
- **asyncpg**: connect to the IPv4 address and use TLS with hostname verification
  relaxed (still verifies the server certificate chain).
"""

from __future__ import annotations

import socket
from urllib.parse import quote, urlparse, urlunparse


def is_supabase_direct_hostname(hostname: str | None) -> bool:
    if not hostname:
        return False
    h = hostname.lower()
    return h.startswith("db.") and h.endswith(".supabase.co")


def first_ipv4(host: str) -> str | None:
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except OSError:
        return None
    if not infos:
        return None
    return infos[0][4][0]


def libpq_append_ipv4_hostaddr(url: str) -> str:
    """Append ``hostaddr`` for ``db.*.supabase.co`` when an A record exists."""
    p = urlparse(url)
    host = p.hostname
    if not is_supabase_direct_hostname(host):
        return url
    q = (p.query or "").lower()
    if "hostaddr=" in q:
        return url
    ipv4 = first_ipv4(host or "")
    if not ipv4:
        return url
    if p.query:
        new_q = f"{p.query}&hostaddr={ipv4}"
    else:
        new_q = f"hostaddr={ipv4}"
    return urlunparse(p._replace(query=new_q))


def asyncpg_url_replace_host_with_ipv4(url: str) -> tuple[str, bool]:
    """
    Replace hostname with IPv4 for asyncpg (it has no ``hostaddr``).

    Returns ``(new_url, needs_relaxed_tls_hostname)``.
    """
    p = urlparse(url)
    host = p.hostname
    if not is_supabase_direct_hostname(host):
        return url, False
    ipv4 = first_ipv4(host or "")
    if not ipv4:
        return url, False

    user = quote(p.username or "", safe="")
    password = p.password
    if password is not None:
        auth = f"{user}:{quote(password, safe='')}"
    elif p.username:
        auth = user
    else:
        auth = ""
    port = f":{p.port}" if p.port else ""
    netloc = f"{auth}@{ipv4}{port}" if auth else f"{ipv4}{port}"
    return urlunparse(p._replace(netloc=netloc)), True


def finalize_libpq_url(canonical_database_url: str, *, use_supabase_ipv4: bool) -> str:
    """
    Build ``postgresql://`` for psycopg2/psycopg3 (Alembic checkpointer, migrations):
    strip asyncpg driver prefix, ensure ``sslmode`` for Supabase, optional ``hostaddr``.
    """
    if canonical_database_url.startswith("postgresql+asyncpg://"):
        sync = canonical_database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    else:
        sync = canonical_database_url
    low = sync.lower()
    if (
        ("supabase.co" in low or "supabase.com" in low)
        and "sslmode=" not in low
        and "ssl=" not in low
    ):
        sync = f"{sync}{'&' if '?' in sync else '?'}sslmode=require"
    if use_supabase_ipv4:
        sync = libpq_append_ipv4_hostaddr(sync)
    return sync

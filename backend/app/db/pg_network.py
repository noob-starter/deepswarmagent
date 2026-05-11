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

import json
import logging
import socket
from urllib.parse import quote, urlparse, urlunparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


def is_supabase_direct_hostname(hostname: str | None) -> bool:
    if not hostname:
        return False
    h = hostname.lower()
    return h.startswith("db.") and h.endswith(".supabase.co")


def first_ipv4_socket(host: str) -> str | None:
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except OSError:
        return None
    if not infos:
        return None
    return infos[0][4][0]


def _ipv4_dns_json(hostname: str, endpoint: str) -> str | None:
    """Resolve A record via DNS-over-HTTPS (no extra deps). Supabase direct is often IPv6-only in local DNS."""
    q = quote(hostname, safe="")
    req = Request(
        f"{endpoint}?name={q}&type=A",
        headers={"Accept": "application/dns-json"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=8) as resp:  # noqa: S310 — fixed DoH URLs only
            raw = resp.read().decode("utf-8")
        payload = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    for ans in payload.get("Answer", []):
        if ans.get("type") == 1 and ans.get("data"):
            return str(ans["data"]).strip()
    return None


def resolve_supabase_direct_ipv4(hostname: str, *, explicit: str | None = None) -> str | None:
    """
    IPv4 for ``hostaddr`` / asyncpg. Order: explicit env, system A record, DoH (Cloudflare, Google).
    """
    if explicit and (t := explicit.strip()):
        return t
    if not hostname:
        return None
    ip = first_ipv4_socket(hostname)
    if ip:
        return ip
    ip = _ipv4_dns_json(hostname, "https://1.1.1.1/dns-query")
    if ip:
        return ip
    return _ipv4_dns_json(hostname, "https://dns.google/resolve")


def libpq_append_ipv4_hostaddr(url: str, *, explicit_ipv4: str | None = None) -> str:
    """Append ``hostaddr`` for ``db.*.supabase.co`` when an IPv4 address is known."""
    p = urlparse(url)
    host = p.hostname
    if not is_supabase_direct_hostname(host):
        return url
    q = (p.query or "").lower()
    if "hostaddr=" in q:
        return url
    ipv4 = resolve_supabase_direct_ipv4(host or "", explicit=explicit_ipv4)
    if not ipv4:
        logger.warning(
            "Could not resolve IPv4 for %s (Supabase direct DNS is often IPv6-only). "
            "Set DATABASE_HOSTADDR or use the Session pooler URI; otherwise deploys on "
            "IPv4-only hosts (e.g. Render) will fail.",
            host,
        )
        return url
    if p.query:
        new_q = f"{p.query}&hostaddr={ipv4}"
    else:
        new_q = f"hostaddr={ipv4}"
    return urlunparse(p._replace(query=new_q))


def asyncpg_url_replace_host_with_ipv4(
    url: str, *, explicit_ipv4: str | None = None
) -> tuple[str, bool]:
    """
    Replace hostname with IPv4 for asyncpg (it has no ``hostaddr``).

    Returns ``(new_url, needs_relaxed_tls_hostname)``.
    """
    p = urlparse(url)
    host = p.hostname
    if not is_supabase_direct_hostname(host):
        return url, False
    ipv4 = resolve_supabase_direct_ipv4(host or "", explicit=explicit_ipv4)
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


def finalize_libpq_url(
    canonical_database_url: str,
    *,
    use_supabase_ipv4: bool,
    explicit_hostaddr: str | None = None,
) -> str:
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
        sync = libpq_append_ipv4_hostaddr(sync, explicit_ipv4=explicit_hostaddr)
    return sync

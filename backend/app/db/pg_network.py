"""
Helpers for hosted Postgres (Supabase direct connection on db.*.supabase.co).

Supabase’s direct host is often **IPv6-only** in public DNS; IPv4-only hosts (e.g. Render)
cannot reach it. Mitigations:

- Resolve an IPv4 for ``hostaddr`` / asyncpg literal host when an A record exists.
- If there is no A record, rewrite to **Supabase Session pooler**
  ``aws-0-{region}.pooler.supabase.com`` with user ``postgres.{project_ref}`` (IPv4).
"""

from __future__ import annotations

import json
import logging
import re
import socket
from urllib.parse import quote, urlparse, urlunparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# AWS region id inside Supabase CNAME targets (pooler host = aws-0-{region}.pooler...).
_AWS_REGION_RE = re.compile(r"\b((?:us|eu|ap|sa|ca|me|af)-[a-z0-9-]+-\d+)\b")


def is_supabase_direct_hostname(hostname: str | None) -> bool:
    if not hostname:
        return False
    h = hostname.lower()
    return h.startswith("db.") and h.endswith(".supabase.co")


def first_ipv4_socket(host: str) -> str | None:
    """Prefer IPv4; try with port hint (some resolvers behave better)."""
    for port in ("5432", None):
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            continue
        for fam, _, _, _, sockaddr in infos:
            if fam == socket.AF_INET:
                return sockaddr[0]
    return None


def _fetch_doh(name: str, rtype: str, endpoint: str) -> dict | None:
    q = quote(name.rstrip("."), safe="")
    req = Request(
        f"{endpoint}?name={q}&type={rtype}",
        headers={"Accept": "application/dns-json"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=8) as resp:  # noqa: S310 — fixed DoH URLs only
            raw = resp.read().decode("utf-8")
        return json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _ipv4_from_doh(hostname: str, endpoint: str, *, depth: int = 5) -> str | None:
    if depth <= 0 or not hostname:
        return None
    payload = _fetch_doh(hostname, "A", endpoint)
    if not payload:
        return None
    try:
        status = int(payload.get("Status", 0))
    except (TypeError, ValueError):
        status = 0
    answers = payload.get("Answer") or []
    if status != 0 and not answers:
        return None
    for ans in answers:
        if ans.get("type") == 1 and ans.get("data"):
            return str(ans["data"]).strip()
    for ans in answers:
        if ans.get("type") == 5 and ans.get("data"):
            target = str(ans["data"]).rstrip(".")
            nested = _ipv4_from_doh(target, endpoint, depth=depth - 1)
            if nested:
                return nested
    return None


def discover_supabase_pooler_region(direct_db_hostname: str) -> str | None:
    """
    Infer ``us-east-1``-style region from CNAME targets of ``db.{ref}.supabase.co``.
    """
    hop = direct_db_hostname.rstrip(".")
    blob_parts = [hop]
    for _ in range(8):
        payload = None
        for ep in (
            "https://1.1.1.1/dns-query",
            "https://dns.google/resolve",
        ):
            payload = _fetch_doh(hop, "CNAME", ep)
            if payload and (payload.get("Answer") or int(payload.get("Status", 0)) == 0):
                break
        if not payload:
            break
        cnames = [
            str(a["data"]).rstrip(".")
            for a in payload.get("Answer") or []
            if a.get("type") == 5 and a.get("data")
        ]
        if not cnames:
            break
        hop = cnames[0]
        blob_parts.append(hop)
    m = _AWS_REGION_RE.search(" ".join(blob_parts))
    return m.group(1) if m else None


def resolve_supabase_direct_ipv4(hostname: str, *, explicit: str | None = None) -> str | None:
    if explicit and (t := explicit.strip()):
        return t
    if not hostname:
        return None
    ip = first_ipv4_socket(hostname)
    if ip:
        return ip
    for ep in (
        "https://1.1.1.1/dns-query",
        "https://dns.google/resolve",
        "https://dns9.quad9.net/dns-query",
    ):
        ip = _ipv4_from_doh(hostname, ep)
        if ip:
            return ip
    return None


def sync_postgresql_url_with_ssl(canonical_database_url: str) -> str:
    """``postgresql://`` + ``sslmode=require`` for Supabase when missing."""
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
    return sync


def libpq_append_ipv4_hostaddr(url: str, *, explicit_ipv4: str | None = None) -> str:
    p = urlparse(url)
    host = p.hostname
    if not is_supabase_direct_hostname(host):
        return url
    q = (p.query or "").lower()
    if "hostaddr=" in q:
        return url
    ipv4 = resolve_supabase_direct_ipv4(host or "", explicit=explicit_ipv4)
    if not ipv4:
        return url
    if p.query:
        new_q = f"{p.query}&hostaddr={ipv4}"
    else:
        new_q = f"hostaddr={ipv4}"
    return urlunparse(p._replace(query=new_q))


def rewrite_supabase_direct_to_session_pooler_sync(
    sync_postgresql_url: str,
    *,
    region_hint: str | None = None,
) -> str | None:
    """
    Rewrite ``db.{ref}.supabase.co`` to Session pooler (port 5432, IPv4).

    Username becomes ``postgres.{ref}`` when the original user is ``postgres``.
    """
    p = urlparse(sync_postgresql_url)
    host = p.hostname
    if not is_supabase_direct_hostname(host):
        return None
    ref = host.removeprefix("db.").removesuffix(".supabase.co")
    region = (region_hint or "").strip() or discover_supabase_pooler_region(host or "")
    if not region:
        return None
    pooler: str | None = None
    for prefix in ("aws-0", "aws-1"):
        candidate = f"{prefix}-{region}.pooler.supabase.com"
        if first_ipv4_socket(candidate):
            pooler = candidate
            break
    if not pooler:
        return None

    raw_user = p.username or "postgres"
    pool_user = f"postgres.{ref}" if raw_user == "postgres" else raw_user
    password = p.password
    uq = quote(pool_user, safe="")
    auth = f"{uq}:{quote(password, safe='')}" if password is not None else uq
    netloc = f"{auth}@{pooler}:5432"
    path = p.path if p.path else "/postgres"
    query = p.query
    if query and "sslmode=" not in query.lower():
        query = f"{query}&sslmode=require"
    elif not query:
        query = "sslmode=require"
    new_url = urlunparse((p.scheme, netloc, path, "", query, ""))
    logger.info(
        "Rewrote Supabase direct URL to session ipv4 session pooler host %s (region=%s).",
        pooler,
        region,
    )
    return new_url


def rewrite_supabase_direct_to_session_pooler_any(
    url: str, *, region_hint: str | None = None
) -> str | None:
    """Same as pooler rewrite; preserves ``postgresql+asyncpg`` scheme if present."""
    p = urlparse(url)
    asyncpg = p.scheme.startswith("postgresql+asyncpg")
    sync = sync_postgresql_url_with_ssl(url)
    out = rewrite_supabase_direct_to_session_pooler_sync(sync, region_hint=region_hint)
    if not out:
        return None
    if asyncpg:
        return out.replace("postgresql://", "postgresql+asyncpg://", 1)
    return out


def asyncpg_url_replace_host_with_ipv4(
    url: str, *, explicit_ipv4: str | None = None
) -> tuple[str, bool]:
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
    embed_hostaddr_in_query: bool = True,
    supabase_pooler_region: str | None = None,
) -> str:
    sync = sync_postgresql_url_with_ssl(canonical_database_url)
    if not use_supabase_ipv4:
        return sync
    if embed_hostaddr_in_query:
        before = sync
        sync = libpq_append_ipv4_hostaddr(sync, explicit_ipv4=explicit_hostaddr)
        needs_pooler = "hostaddr=" not in (urlparse(sync).query or "").lower()
        if needs_pooler and is_supabase_direct_hostname(urlparse(sync).hostname):
            pool = rewrite_supabase_direct_to_session_pooler_sync(
                sync, region_hint=supabase_pooler_region
            )
            if pool:
                return pool
        if needs_pooler and sync == before:
            logger.warning(
                "No IPv4 for %s and pooler rewrite failed; set SUPABASE_POOLER_REGION, "
                "use Session pooler DATABASE_URL from Supabase, or DATABASE_HOSTADDR.",
                urlparse(sync).hostname,
            )
        return sync
    return sync

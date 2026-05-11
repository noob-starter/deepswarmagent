"""
HTTP “browser lite” fetcher — Playwright-free for smaller containers.

Fetches raw HTML and extracts visible text with a conservative regex strip.

Full browser workers can swap this module for Playwright without
changing LangGraph node contracts.
"""

from __future__ import annotations

import re
from html import unescape

import httpx

from app.config import get_settings


_WS = re.compile(r"\s+")


def _strip_html(html: str, max_chars: int) -> str:
    """Very small HTML → text; not a DOM parser, but OK for trust heuristics."""
    # Remove script/style blocks crudely
    html = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style.*?>.*?</style>", " ", html)
    html = re.sub(r"(?is)<noscript.*?>.*?</noscript>", " ", html)
    # Drop tags
    text = re.sub(r"(?s)<.*?>", " ", html)
    text = unescape(text)
    text = _WS.sub(" ", text).strip()
    return text[:max_chars]


async def fetch_url_text(url: str) -> str | None:
    """
    Return cleaned page text or ``None`` on failure / non-text content.

    Respects ``fetch_max_bytes`` and ``fetch_timeout_seconds`` from settings.
    """
    settings = get_settings()
    headers = {"User-Agent": settings.user_agent, "Accept": "text/html,*/*;q=0.8"}
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=settings.fetch_timeout_seconds,
            headers=headers,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "").lower()
            if "text/html" not in ctype and "text/plain" not in ctype:
                return None
            raw = resp.content[: settings.fetch_max_bytes]
            text = raw.decode(resp.encoding or "utf-8", errors="ignore")
            if "text/plain" in ctype:
                return text.strip()[:8000]
            return _strip_html(text, 12_000)
    except Exception:
        return None

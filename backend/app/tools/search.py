"""
Search tools: optionally upgrade with API keys; DuckDuckGo as last resort.

- If **both** ``TAVILY_API_KEY`` and ``SERPER_API_KEY`` are set, both run in parallel
  and hits are merged with ~equal alternation by source (URLs dedupe).
- Otherwise tiered fallback: Tavily → Serper → DuckDuckGo text search via
  ``duckduckgo-search`` (no key).
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from typing import Any

import httpx
from duckduckgo_search import DDGS

from app.config import get_settings
from app.schemas.state import SourceDict
from app.tools.registry import wrap_sources

# When Tavily + Serper both run, cap blended list length (overlap dedupes URLs).
_DUAL_MERGED_MAX_RESULTS = 12
_MERGED_HARD_CAP = 18


def _merge_sources_by_url(
    first: list[SourceDict],
    second: list[SourceDict],
    *,
    max_total: int,
) -> list[SourceDict]:
    out: list[SourceDict] = list(first)
    seen: set[str] = set()
    for s in out:
        uk = _url_key(str(s.get("url") or ""))
        if uk:
            seen.add(uk)
    for s in second:
        uk = _url_key(str(s.get("url") or ""))
        if not uk or uk in seen:
            continue
        seen.add(uk)
        out.append(s)
        if len(out) >= max_total:
            break
    return out


def _url_key(url: str) -> str:
    return url.strip().rstrip("/").lower()


def _interleave_dual_sources(
    tavily_sources: list[SourceDict],
    serper_sources: list[SourceDict],
    *,
    max_results: int = _DUAL_MERGED_MAX_RESULTS,
) -> list[SourceDict]:
    """
    Alternate Tavily ↔ Serper for each new URL so both providers contribute equally
    when both return rows (URLs seen once globally).
    """
    seen: set[str] = set()
    out: list[SourceDict] = []

    def _take_next(lst: list[SourceDict], start: int) -> tuple[SourceDict | None, int]:
        i = start
        while i < len(lst):
            s = lst[i]
            i += 1
            uk = _url_key(s["url"])
            if uk not in seen:
                seen.add(uk)
                return s, i
        return None, i

    i_t, i_s = 0, 0
    use_tavily = True
    while len(out) < max_results:
        if i_t >= len(tavily_sources) and i_s >= len(serper_sources):
            break
        item: SourceDict | None = None
        if use_tavily and i_t < len(tavily_sources):
            item, i_t = _take_next(tavily_sources, i_t)
        elif not use_tavily and i_s < len(serper_sources):
            item, i_s = _take_next(serper_sources, i_s)
        elif i_t < len(tavily_sources):
            item, i_t = _take_next(tavily_sources, i_t)
        elif i_s < len(serper_sources):
            item, i_s = _take_next(serper_sources, i_s)

        use_tavily = not use_tavily

        if item is not None:
            out.append(item)

    return out


async def search_duckduckgo(query: str, session_id: str) -> list[dict[str, Any]]:
    """DuckDuckGo text results — no API key (subject to provider rate limits)."""

    def _run() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            with DDGS() as ddgs:
                for item in ddgs.text(query, max_results=6):
                    rows.append(
                        {
                            "url": item.get("href"),
                            "title": item.get("title"),
                            "snippet": item.get("body"),
                            "published_date": None,
                            "domain_authority": 50.0,
                        }
                    )
        except Exception:
            return rows
        return rows

    return await asyncio.to_thread(_run)


async def search_tavily(query: str, session_id: str) -> list[dict[str, Any]]:
    key = get_settings().tavily_api_key
    if not key:
        return []
    async with httpx.AsyncClient(timeout=40.0) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": key,
                "query": query,
                "search_depth": "basic",
                "include_answer": False,
                "max_results": 8,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    results = data.get("results") or []
    out: list[dict[str, Any]] = []
    for r in results:
        out.append(
            {
                "url": r.get("url"),
                "title": r.get("title"),
                "snippet": r.get("content") or r.get("snippet") or "",
                "published_date": r.get("published_date"),
                "domain_authority": 60.0,
            }
        )
    return out


async def search_serper(query: str, session_id: str) -> list[dict[str, Any]]:
    key = get_settings().serper_api_key
    if not key:
        return []
    async with httpx.AsyncClient(timeout=40.0) as client:
        resp = await client.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": key},
            json={"q": query, "num": 8},
        )
        resp.raise_for_status()
        data = resp.json()
    organic = data.get("organic") or []
    out: list[dict[str, Any]] = []
    for r in organic:
        out.append(
            {
                "url": r.get("link"),
                "title": r.get("title"),
                "snippet": r.get("snippet") or "",
                "published_date": None,
                "domain_authority": 58.0,
            }
        )
    return out


async def unified_web_search(query: str, session_id: str) -> list[SourceDict]:
    """
    Dual-key mode runs Tavily and Serper in parallel and merges with equal-ish
    alternation; otherwise tiered Tavily → Serper → DuckDuckGo.

    This is the default ``search_web`` tool implementation.
    """
    settings = get_settings()
    dual = bool(settings.tavily_api_key and settings.serper_api_key)

    if dual:
        t_raw, s_raw = await asyncio.gather(
            search_tavily(query, session_id),
            search_serper(query, session_id),
            return_exceptions=True,
        )
        t_list: list[dict[str, Any]] = [] if isinstance(t_raw, Exception) else list(t_raw)
        s_list: list[dict[str, Any]] = [] if isinstance(s_raw, Exception) else list(s_raw)
        merged = _interleave_dual_sources(
            wrap_sources(t_list, tool_name="tavily", session_id=session_id),
            wrap_sources(s_list, tool_name="serper", session_id=session_id),
            max_results=_DUAL_MERGED_MAX_RESULTS,
        )
        if merged:
            base = merged
            if settings.search_archive_supplement:
                try:
                    arc_raw = await search_duckduckgo(
                        f"{query} site:web.archive.org",
                        session_id,
                    )
                    arc = wrap_sources(
                        arc_raw[:6],
                        tool_name="internet_archive",
                        session_id=session_id,
                    )
                    if arc:
                        base = _merge_sources_by_url(base, arc, max_total=_MERGED_HARD_CAP)
                except Exception:
                    pass
            return base
        raw = await search_duckduckgo(query, session_id)
        if not raw:
            return []
        base = wrap_sources(raw, tool_name="duckduckgo", session_id=session_id)
        if settings.search_archive_supplement:
            try:
                arc_raw = await search_duckduckgo(
                    f"{query} site:web.archive.org",
                    session_id,
                )
                arc = wrap_sources(
                    arc_raw[:6],
                    tool_name="internet_archive",
                    session_id=session_id,
                )
                if arc:
                    base = _merge_sources_by_url(base, arc, max_total=_MERGED_HARD_CAP)
            except Exception:
                pass
        return base

    raw: list[dict[str, Any]] = []
    if settings.tavily_api_key:
        raw = await search_tavily(query, session_id)
    if raw:
        base = wrap_sources(raw, tool_name="tavily", session_id=session_id)
        if settings.search_archive_supplement:
            try:
                arc_raw = await search_duckduckgo(
                    f"{query} site:web.archive.org",
                    session_id,
                )
                arc = wrap_sources(
                    arc_raw[:6],
                    tool_name="internet_archive",
                    session_id=session_id,
                )
                if arc:
                    base = _merge_sources_by_url(base, arc, max_total=_MERGED_HARD_CAP)
            except Exception:
                pass
        return base

    if settings.serper_api_key:
        raw = await search_serper(query, session_id)
    if raw:
        base = wrap_sources(raw, tool_name="serper", session_id=session_id)
        if settings.search_archive_supplement:
            try:
                arc_raw = await search_duckduckgo(
                    f"{query} site:web.archive.org",
                    session_id,
                )
                arc = wrap_sources(
                    arc_raw[:6],
                    tool_name="internet_archive",
                    session_id=session_id,
                )
                if arc:
                    base = _merge_sources_by_url(base, arc, max_total=_MERGED_HARD_CAP)
            except Exception:
                pass
        return base

    raw = await search_duckduckgo(query, session_id)
    if not raw:
        if settings.search_archive_supplement:
            raw = await search_duckduckgo(
                f"{query} site:web.archive.org",
                session_id,
            )
        if not raw:
            return []
    base = wrap_sources(raw, tool_name="duckduckgo", session_id=session_id)
    if settings.search_archive_supplement:
        try:
            arc_raw = await search_duckduckgo(
                f"{query} (site:web.archive.org OR site:archive.org)",
                session_id,
            )
            arc = wrap_sources(
                arc_raw[:6],
                tool_name="internet_archive",
                session_id=session_id,
            )
            if arc:
                base = _merge_sources_by_url(base, arc, max_total=_MERGED_HARD_CAP)
        except Exception:
            pass
    return base


async def arxiv_lite_search(query: str, session_id: str) -> list[SourceDict]:
    """
    Lightweight arXiv API search without extra dependencies.

    If the query returns nothing, falls back to ``unified_web_search``.
    """
    url = "https://export.arxiv.org/api/query"
    params = {"search_query": f"all:{query}", "start": 0, "max_results": 5}
    async with httpx.AsyncClient(
        timeout=40.0,
        headers={"User-Agent": get_settings().user_agent},
    ) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        text = resp.text
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    rows: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(text)
        for entry in root.findall("atom:entry", ns):
            title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
            link_el = entry.find('atom:link[@rel="alternate"]', ns)
            href = link_el.get("href") if link_el is not None else ""
            summary = (entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()
            published = (entry.findtext("atom:published", default="", namespaces=ns) or "").strip()
            if href:
                rows.append(
                    {
                        "url": href,
                        "title": title or "arXiv entry",
                        "snippet": summary[:2000],
                        "published_date": published,
                        "domain_authority": 82.0,
                    }
                )
    except ET.ParseError:
        rows = []
    if not rows:
        return await unified_web_search(query, session_id)
    return wrap_sources(rows, tool_name="arxiv", session_id=session_id)


async def github_hint_search(query: str, session_id: str) -> list[SourceDict]:
    """
    Without a GitHub token, bias retrieval toward ``github.com`` via query rewriting.

    Preserves a distinct “code rail” for tool routing in the planner.
    """
    settings = get_settings()
    enriched = f"{query} site:github.com"
    raw = await search_duckduckgo(enriched, session_id)
    if not raw and settings.tavily_api_key:
        raw = await search_tavily(enriched, session_id)
    if not raw:
        return await unified_web_search(query, session_id)
    return wrap_sources(raw, tool_name="github_hint", session_id=session_id)

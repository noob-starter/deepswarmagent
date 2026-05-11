"""
Register tool outputs under one schema (`schemas.state.SourceDict`).

The planner can assign tool subsets per sub-question; the registry exposes
callables used by Searcher workers.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.schemas.state import SourceDict

ToolFn = Callable[[str, str], Awaitable[list[SourceDict]]]


def stable_source_id(url: str, title: str, session_id: str) -> str:
    """Deterministic id so the same URL reused in-session collapses cleanly."""
    h = hashlib.sha256(f"{session_id}|{url}|{title}".encode()).hexdigest()[:16]
    return f"src_{h}"


def wrap_sources(
    raw: list[dict[str, Any]],
    *,
    tool_name: str,
    session_id: str,
) -> list[SourceDict]:
    """Normalize provider-specific records to ``SourceDict``."""
    out: list[SourceDict] = []
    for row in raw:
        url = str(row.get("url") or "").strip()
        title = str(row.get("title") or "Untitled").strip()
        if not url:
            continue
        sid = row.get("source_id") or stable_source_id(url, title, session_id)
        out.append(
            {
                "source_id": sid,
                "url": url,
                "title": title,
                "snippet": str(row.get("snippet") or "")[:2000],
                "full_content": row.get("full_content"),
                "published_date": row.get("published_date"),
                "domain_authority": float(row.get("domain_authority") or 50.0),
                "tool_name": tool_name,
            }
        )
    return out


def make_tool_registry(
    *,
    search_web: ToolFn,
    search_academic: ToolFn | None = None,
    search_code: ToolFn | None = None,
) -> dict[str, ToolFn]:
    """
    Map logical tool name → async implementation.

    Args:
        search_web: default web/news style retrieval.
        search_academic: optional ArXiv / scientific bias (may reuse web).
        search_code: optional GitHub bias (may reuse web until API keys exist).
    """
    reg: dict[str, ToolFn] = {
        "search_web": search_web,
    }
    if search_academic:
        reg["search_academic"] = search_academic
    if search_code:
        reg["search_code"] = search_code
    return reg


def new_sub_question_id() -> str:
    return str(uuid.uuid4())

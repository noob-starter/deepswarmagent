"""
Parallel Searcher workers (Tier 2) — context isolation inside one graph node.

We intentionally gather asyncio tasks here so a single LangGraph
step encapsulates the full wave; this keeps Postgres/session semantics simple
while preserving bounded concurrency via ``asyncio.Semaphore``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from uuid import UUID

from app.config import get_settings
from app.graph.nodes._util import budget_exceeded, tools_for_subq, wrap_untrusted
from app.schemas.state import ResearchGraphState, SourceDict
from app.services.llm import chat_text
from app.services.session_events import append_event
from app.tools.fetch import fetch_url_text
from app.tools.search import arxiv_lite_search, github_hint_search, unified_web_search

ToolRouter = dict[str, Callable[..., Coroutine[Any, Any, list[SourceDict]]]]


def _router() -> ToolRouter:
    return {
        "search_web": unified_web_search,
        "search_academic": arxiv_lite_search,
        "search_code": github_hint_search,
    }


async def _search_one_subq(
    subq: dict[str, Any],
    session_id: str,
    sem: asyncio.Semaphore,
    settings: Any,
) -> tuple[list[str], list[SourceDict], float, int]:
    """
    Run tool calls + summarization for a single sub-question.

    Returns:
        summaries: list of one string
        sources: list of SourceDict
        cost: USD from summarizer LLM
        invocations: LLM call count for this worker (usually 1)
    """
    router = _router()
    text = str(subq.get("text") or "")
    tools = tools_for_subq(subq)

    collected: list[SourceDict] = []
    tool_calls = 0
    async with sem:
        for tool_name in tools:
            if tool_calls >= settings.max_tool_calls_per_agent_invocation:
                break
            fn = router.get(tool_name) or unified_web_search
            batch: list[SourceDict] = []
            try:
                batch = await fn(text, session_id)
                collected.extend(batch)
            except Exception:
                batch = []
            finally:
                tool_calls += 1
            try:
                await append_event(
                    UUID(session_id),
                    "tool_call",
                    {
                        "agent_id": str(subq.get("id") or "searcher"),
                        "parent_id": "parallel_search",
                        "tool": tool_name,
                        "args_summary": text[:240],
                        "hits": len(batch),
                    },
                )
            except Exception:
                pass

    # Optional shallow fetch for the top URL (HTTP-only “browser lite”).
    if collected and tool_calls < settings.max_tool_calls_per_agent_invocation:
        top = collected[0]
        url = top.get("url") or ""
        if url:
            page = await fetch_url_text(url)
            tool_calls += 1
            if page:
                collected[0] = {
                    **top,
                    "full_content": page[:8000],
                }

    snippets_lines = []
    for s in collected[:8]:
        snippets_lines.append(
            f"- {s.get('title')} ({s.get('url')}): {wrap_untrusted(str(s.get('snippet') or '')[:500])}"
        )
    block = "\n".join(snippets_lines) if snippets_lines else "No sources retrieved."

    system = (
        "You are a careful research assistant. Given untrusted excerpts, write a "
        "200–500 token summary answering ONLY the sub-question. "
        "Note conflicts if sources disagree. Do not invent URLs."
    )
    user = f"Sub-question: {text}\nSources:\n{block}"
    summary, cost = await chat_text(
        model=settings.model_fast,
        system=system,
        user=user,
        max_tokens=896,
        temperature=0.22,
        top_p=0.9,
        session_id=session_id,
        generation_name="search_summarize",
    )
    return [summary], collected, cost, 1


async def parallel_search_node(state: ResearchGraphState) -> dict[str, Any]:
    """Execute one wave over ``pending_sub_questions``."""
    if budget_exceeded(state):
        return {
            "pending_sub_questions": [],
            "messages": ["Budget exceeded; skipping search wave."],
            "stop_reason": "budget",
        }

    settings = get_settings()
    pending = list(state.get("pending_sub_questions") or [])
    if not pending:
        return {"messages": ["No pending sub-questions; skipping search wave."]}

    sem = asyncio.Semaphore(settings.max_parallel_agent_calls)
    tasks = [_search_one_subq(subq, state["session_id"], sem, settings) for subq in pending]
    results = await asyncio.gather(*tasks)

    summaries: list[str] = []
    sources_flat: list[SourceDict] = []
    total_cost = 0.0
    total_inv = 0
    for summ_list, src_list, cst, inv in results:
        summaries.extend(summ_list)
        sources_flat.extend(src_list)
        total_cost += float(cst)
        total_inv += int(inv)

    return {
        "pending_sub_questions": [],  # consumed
        "findings_summaries": summaries,
        "all_sources": sources_flat,
        "agent_invocations": total_inv,
        "cost_usd": total_cost,
        "messages": [f"Search wave completed ({len(summaries)} summaries)."],
    }

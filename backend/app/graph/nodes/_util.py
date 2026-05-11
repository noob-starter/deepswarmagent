"""Shared helpers for LangGraph nodes (budget, prompts, tooling)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.config import get_settings
from app.schemas.state import ResearchGraphState


def budget_exceeded(state: ResearchGraphState) -> bool:
    """Return True when the session has crossed configured USD limits."""
    settings = get_settings()
    limit = settings.session_cost_limit_usd
    if limit <= 0:
        return False  # zero or negative = no cap (see SESSION_COST_LIMIT_USD)
    spent = Decimal(str(state.get("cost_usd") or 5.0))
    return spent >= limit


def wrap_untrusted(text: str) -> str:
    """
    Fence raw web-derived text to reduce prompt-injection influence.

    Downstream LLM instructions should treat content inside the fence as data.
    """
    return f"<untrusted_web_content>\n{text}\n</untrusted_web_content>"


def truncate_chars(text: str, max_chars: int) -> str:
    """Bound prompt size for local LLMs (speed + fewer timeouts)."""
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head - 40
    return text[:head] + "\n… [middle truncated] …\n" + text[-tail:]


def tools_for_subq(subq: dict[str, Any]) -> list[str]:
    """Resolve tool rail names from planner output with safe defaults."""
    t = subq.get("tools") or subq.get("assigned_tools") or ["search_web"]
    if not isinstance(t, list):
        return ["search_web"]
    return [str(x) for x in t][:4]

"""
Critic agent (Tier 3 — quality control).

Surfaces follow-up sub-questions when sourcing or reasoning looks thin.
Bounded by ``MAX_CRITIC_ROUNDS`` in settings via the graph router.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.graph.nodes._util import budget_exceeded, truncate_chars, wrap_untrusted
from app.schemas.state import ResearchGraphState
from app.services.llm import chat_json


async def critic_node(state: ResearchGraphState) -> dict[str, Any]:
    """Append follow-up questions to ``critic_followups`` for routing."""
    if budget_exceeded(state):
        return {
            "critic_followups": [],
            "messages": ["Budget exceeded; critic skipped."],
            "stop_reason": "budget",
        }

    settings = get_settings()
    findings_raw = "\n\n".join(state.get("findings_summaries") or []) or "(none)"
    findings = truncate_chars(findings_raw, 14_000)
    user = (
        f"Original query: {state['user_query']}\n\n"
        f"Findings so far:\n{wrap_untrusted(findings)}\n\n"
        "Return JSON: followups (array of short strings, may be empty), "
        "satisfied (boolean) if findings are enough to answer the query."
    )
    system = (
        "You critique research coverage. Output ONLY JSON. "
        "If there are contradictions, missing primary sources, or unclear scope, "
        "add concise followups (max 5). Never instruct executing code."
    )
    data, cost = await chat_json(
        model=settings.model_strong,
        system=system,
        user=user,
        max_tokens=768,
        session_id=str(state["session_id"]),
        generation_name="critic",
    )
    follow: list[str] = []
    if isinstance(data, dict):
        raw = data.get("followups") or []
        if isinstance(raw, list):
            follow = [str(x).strip() for x in raw if str(x).strip()][:5]

    return {
        "critic_followups": follow,
        "agent_invocations": 1,
        "cost_usd": cost,
        "messages": [f"Critic emitted {len(follow)} follow-ups."],
    }

"""
Planner agent (Tier 1 — coordination).

Produces a structured research plan JSON: hypothesis tree, sub-questions (3–8),
and per-sub-question tool routing (web / academic / code).
"""

from __future__ import annotations

import uuid
from typing import Any

from app.config import get_settings
from app.graph.nodes._util import budget_exceeded
from app.schemas.state import ResearchGraphState
from app.services.llm import chat_json


async def planner_node(state: ResearchGraphState) -> dict[str, Any]:
    """
    Initialize ``pending_sub_questions`` from the LLM plan.

    On budget blowout, emit a minimal plan so the graph can still exit cleanly.
    """
    if budget_exceeded(state):
        return {
            "plan": {"error": "budget_exceeded_before_planner"},
            "pending_sub_questions": [],
            "messages": ["Budget exceeded before planner."],
            "stop_reason": "budget",
        }

    settings = get_settings()
    system = (
        "You are a research planner. Output ONLY valid JSON with keys: "
        "hypothesis_tree (string), sub_questions (array of objects with "
        "fields text (string) and tools (array of strings)). "
        "tools must be chosen from: search_web, search_academic, search_code. "
        "Use 3 to 8 sub_questions. Keep text concise."
    )
    user = f"User query: {state['user_query']}"
    data, cost = await chat_json(
        model=settings.model_strong,
        system=system,
        user=user,
        max_tokens=896,
        session_id=str(state["session_id"]),
        generation_name="planner",
    )
    invocations = 1

    subqs_raw = []
    if isinstance(data, dict):
        subqs_raw = data.get("sub_questions") or []

    pending: list[dict[str, Any]] = []
    for item in subqs_raw[: settings.max_sub_questions_per_wave]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        tools = item.get("tools")
        if not isinstance(tools, list) or not tools:
            tools = ["search_web"]
        pending.append(
            {
                "id": str(uuid.uuid4()),
                "text": text,
                "tools": [str(t) for t in tools][:4],
                "status": "pending",
            }
        )

    if not pending:
        # Degenerate model output — single fallback sub-question
        pending.append(
            {
                "id": str(uuid.uuid4()),
                "text": state["user_query"],
                "tools": ["search_web"],
                "status": "pending",
            }
        )

    plan_dict = data if isinstance(data, dict) else {"raw": data}
    return {
        "plan": plan_dict,
        "pending_sub_questions": pending,
        "agent_invocations": invocations,
        "cost_usd": cost,
        "messages": [f"Planner produced {len(pending)} sub-questions."],
    }

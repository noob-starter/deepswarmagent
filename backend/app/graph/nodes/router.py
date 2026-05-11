"""
Graph routing after the critic — implements bounded critic→search loops.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from app.config import get_settings
from app.schemas.state import ResearchGraphState


def after_critic(state: ResearchGraphState) -> Literal["search_again", "extract_claims"]:
    """
    Decide whether to spawn another search wave or proceed to claim extraction.

    Side effect: when looping, this function mutates routing counters by
    returning instructions via a dedicated node — we handle state updates in
    ``route_node`` below to keep LangGraph transitions pure.
    """
    settings = get_settings()
    follow = state.get("critic_followups") or []
    round_idx = int(state.get("critic_round") or 0)
    if follow and round_idx < settings.max_critic_rounds:
        return "search_again"
    return "extract_claims"


async def critic_route_prepare_node(state: ResearchGraphState) -> dict[str, Any]:
    """
    When ``after_critic`` selects ``search_again``, materialize pending work.

    This node sits on the ``search_again`` edge before ``parallel_search``.
    The conditional edge function cannot mutate state; we use this helper node
    for increments and queue filling.
    """

    settings = get_settings()
    follow = list(state.get("critic_followups") or [])
    round_idx = int(state.get("critic_round") or 0)
    if not follow or round_idx >= settings.max_critic_rounds:
        return {}

    pending = []
    for f in follow[: settings.max_sub_questions_per_wave]:
        pending.append(
            {
                "id": str(uuid.uuid4()),
                "text": f,
                "tools": ["search_web"],
                "status": "pending",
            }
        )
    return {
        "pending_sub_questions": pending,
        "critic_round": round_idx + 1,
        "critic_followups": [],
        "messages": [f"Queueing critic wave {round_idx + 1} with {len(pending)} tasks."],
    }

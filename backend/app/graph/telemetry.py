"""
Wrap LangGraph node callables with telemetry (starts + completions).

Keeps node modules free of boilerplate while preserving structured SSE events.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable
from uuid import UUID

from app.schemas.state import ResearchGraphState
from app.services.session_events import append_event

logger = logging.getLogger(__name__)

NodeFn = Callable[[ResearchGraphState], Awaitable[dict[str, Any]]]


def wrap_node(agent_id: str, fn: NodeFn, *, label: str | None = None) -> NodeFn:
    """
    Emit ``agent_started`` before invocation and ``agent_completed`` after.

    Failures still emit ``agent_completed`` with an ``error`` field for the UI.
    """

    pretty = label or agent_id.replace("_", " ").title()

    async def _wrapped(state: ResearchGraphState) -> dict[str, Any]:
        sid = UUID(state["session_id"])
        await append_event(
            sid,
            "agent_started",
            {
                "agent_id": agent_id,
                "parent_id": "orchestrator",
                "label": pretty,
            },
        )
        try:
            out = await fn(state)
            await append_event(
                sid,
                "agent_completed",
                {
                    "agent_id": agent_id,
                    "ok": True,
                    "updated_keys": sorted((out or {}).keys()),
                },
            )
            return out or {}
        except Exception as exc:  # noqa: BLE001 — must surface to UI + LangGraph
            logger.exception("Node %s failed", agent_id)
            await append_event(
                sid,
                "agent_completed",
                {"agent_id": agent_id, "ok": False, "error": str(exc)},
            )
            raise

    return _wrapped

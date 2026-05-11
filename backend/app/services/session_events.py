"""
Event bus — durable log in Postgres + SSE delivery.

Event types (contract):
- ``run_config`` (models + limits at run start)
- ``agent_started`` / ``agent_completed``
- ``tool_call``
- ``claim_verified``
- ``cost_update``

Clients reconnect by passing the last seen monotonic ``id`` (via ``?after_id=``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import ResearchEvent, ResearchSession
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)


async def append_event(
    session_id: UUID,
    event_type: str,
    payload: dict | None = None,
    *,
    db: AsyncSession | None = None,
) -> ResearchEvent:
    """
    Persist one event. Prefer injecting ``db`` when already inside a transaction.

    By default opens a short-lived session — OK for moderate fan-out volume.
    """
    body = dict(payload or {})
    if db is not None:
        ev = ResearchEvent(session_id=session_id, event_type=event_type, payload=body)
        db.add(ev)
        await db.flush()
        return ev

    async with AsyncSessionLocal() as session:
        ev = ResearchEvent(session_id=session_id, event_type=event_type, payload=body)
        session.add(ev)
        await session.commit()
        await session.refresh(ev)
        return ev


async def load_events_after(
    db: AsyncSession,
    session_id: UUID,
    after_id: int,
    limit: int,
) -> list[ResearchEvent]:
    """Read the next ``limit`` events strictly after ``after_id`` (for replay)."""
    res = await db.execute(
        select(ResearchEvent)
        .where(ResearchEvent.session_id == session_id, ResearchEvent.id > after_id)
        .order_by(ResearchEvent.id)
        .limit(limit),
    )
    return list(res.scalars().all())


async def list_session_events(
    db: AsyncSession,
    session_id: UUID,
    *,
    limit: int = 10_000,
) -> list[dict[str, object]]:
    """
    Ordered audit log slice for dashboards / exports (SSE clients often truncate buffers).

    ``limit`` is capped for safety against accidental huge allocations.
    """
    cap = max(1, min(limit, 50_000))
    res = await db.execute(
        select(ResearchEvent)
        .where(ResearchEvent.session_id == session_id)
        .order_by(ResearchEvent.id)
        .limit(cap),
    )
    out: list[dict[str, object]] = []
    for ev in res.scalars():
        out.append(
            {
                "id": ev.id,
                "event_type": ev.event_type,
                "payload": dict(ev.payload),
                "created_at": ev.created_at.isoformat() if ev.created_at else None,
            }
        )
    return out


def _sse_data_line(ev: ResearchEvent) -> str:
    blob = {
        "id": ev.id,
        "event_type": ev.event_type,
        "payload": ev.payload,
        "created_at": ev.created_at.isoformat() if ev.created_at else None,
    }
    return f"data: {json.dumps(blob, default=str)}\n\n"


async def sse_event_stream(
    session_id: UUID,
    *,
    after_id: int = 0,
    replay_limit: int | None = None,
) -> AsyncIterator[str]:
    """
    Poll the ``research_events`` table and yield SSE frames.

    Works across multiple API workers (polling + DB ordering) at the cost of
    ~sub-second latency. Pair with ``after_id`` reconnect semantics.
    """
    settings = get_settings()
    cap = replay_limit or settings.sse_replay_max_events
    last_sent = after_id
    terminal_sleep_ticks = 0

    while True:
        async with AsyncSessionLocal() as db:
            row = await db.get(ResearchSession, session_id)
            if row is None:
                yield 'data: {"event_type":"error","payload":{"message":"session_not_found"}}\n\n'
                return

            batch = await load_events_after(db, session_id, last_sent, min(cap, 200))
            status = row.status

        for ev in batch:
            last_sent = ev.id
            yield _sse_data_line(ev)

        is_terminal = status in ("completed", "failed")
        if is_terminal:
            if not batch:
                terminal_sleep_ticks += 1
                if terminal_sleep_ticks >= 2:
                    yield f"data: {json.dumps({'event_type': 'session_status', 'payload': {'status': status}})}\n\n"
                    return
            else:
                terminal_sleep_ticks = 0
        else:
            terminal_sleep_ticks = 0

        await asyncio.sleep(settings.sse_poll_interval_seconds)

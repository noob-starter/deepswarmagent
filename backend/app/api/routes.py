"""HTTP routes (REST + SSE for the React dashboard)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResearchSession
from app.db.session import get_db
from app.schemas.api import (
    ResearchCreateRequest,
    ResearchSessionDetailResponse,
    ResearchSessionResponse,
)
from app.services.research_runner import load_session, run_research_job
from app.services.session_events import list_session_events, sse_event_stream

router = APIRouter(prefix="/api/v1", tags=["research"])


def _to_summary(row: ResearchSession) -> ResearchSessionResponse:
    return ResearchSessionResponse(
        id=row.id,
        query=row.query,
        status=row.status,
        total_cost_usd=row.total_cost_usd,
        agent_invocation_count=row.agent_invocation_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_detail(row: ResearchSession) -> ResearchSessionDetailResponse:
    return ResearchSessionDetailResponse(
        **_to_summary(row).model_dump(),
        final_report=row.final_report,
        error_message=row.error_message,
        graph_state=row.graph_state,
    )


@router.post(
    "/research",
    response_model=ResearchSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_research(
    body: ResearchCreateRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> ResearchSessionResponse:
    """
    Enqueue a multi-agent research run.

    The HTTP request returns immediately with ``status=pending``; poll
    ``GET /api/v1/research/{id}`` until ``completed`` or ``failed``.
    """
    row = ResearchSession(query=body.query.strip(), status="pending")
    db.add(row)
    await db.flush()
    session_id = row.id
    await db.commit()
    background_tasks.add_task(run_research_job, session_id)
    refreshed = await db.get(ResearchSession, session_id)
    if refreshed is None:
        raise HTTPException(status_code=500, detail="Failed to persist session.")
    return _to_summary(refreshed)


@router.get("/research/{session_id}", response_model=ResearchSessionDetailResponse)
async def get_research(
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> ResearchSessionDetailResponse:
    """Return session accounting fields plus optional report / debug graph state."""
    row = await load_session(db, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return _to_detail(row)


@router.get("/research/{session_id}/events")
async def get_research_events(
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(
        default=20_000,
        ge=1,
        le=50_000,
        description="Max rows returned (SSE clients often keep only recent events in-memory).",
    ),
) -> list[dict]:
    """Return persisted audit events for dashboards and PDF exports (full history, capped)."""
    row = await load_session(db, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return await list_session_events(db, session_id, limit=limit)


@router.get("/research/{session_id}/stream")
async def stream_research_events(
    session_id: UUID,
    after_id: int = Query(default=0, ge=0, description="Monotonic cursor for reconnect replay."),
    replay_limit: int = Query(default=500, ge=1, le=2000),
) -> StreamingResponse:
    """
    Server-Sent Events stream of ``research_events`` rows.

    Clients should keep the highest ``id`` they've processed and reconnect with
    ``?after_id=`` after drops. Polling interval is configured via ``SSE_POLL_INTERVAL_SECONDS``.
    """
    return StreamingResponse(
        sse_event_stream(session_id, after_id=after_id, replay_limit=replay_limit),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/research/{session_id}/resume", response_model=ResearchSessionResponse)
async def resume_research(
    session_id: UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> ResearchSessionResponse:
    """
    Continue a session from the latest LangGraph Postgres checkpoint.

    Allowed when the last attempt ``failed`` or appears ``running`` after an API
    worker crash. Completed sessions are rejected.
    """
    row = await load_session(db, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if row.status == "completed":
        raise HTTPException(status_code=400, detail="Session already completed.")
    if row.status not in {"failed", "running"}:
        raise HTTPException(
            status_code=400,
            detail="Only failed or running sessions can be resumed from checkpoints.",
        )

    row.status = "running"
    await db.commit()

    async def _resume() -> None:
        await run_research_job(session_id, resume=True)

    background_tasks.add_task(_resume)
    refreshed = await load_session(db, session_id)
    if refreshed is None:
        raise HTTPException(status_code=500, detail="Session disappeared during resume.")
    return _to_summary(refreshed)

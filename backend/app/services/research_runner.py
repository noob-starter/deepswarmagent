"""
Execute a research session persisted in Postgres.

Uses LangGraph **Postgres checkpointer** (same database URL as SQLAlchemy, sync
`postgresql://` form for ``psycopg``) plus ``astream(..., stream_mode="values")``
to emit ``cost_update`` events as reducers advance state.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import ResearchSession
from app.db.pg_network import finalize_libpq_url
from app.db.session import AsyncSessionLocal
from app.graph.build import compile_research_graph
from app.schemas.state import ResearchGraphState
from app.services.session_events import append_event

logger = logging.getLogger(__name__)


def _checkpoint_conn_string() -> str:
    """LangGraph's AsyncPostgresSaver expects a libpq URI without asyncpg driver."""
    s = get_settings()
    return finalize_libpq_url(s.database_url, use_supabase_ipv4=s.database_supabase_ipv4)


def _initial_state(session_id: UUID, query: str) -> ResearchGraphState:
    """Seed LangGraph with empty accumulators for reducer-backed fields."""
    return {
        "session_id": str(session_id),
        "user_query": query,
        "pending_sub_questions": [],
        "findings_summaries": [],
        "all_sources": [],
        "critic_round": 0,
        "critic_followups": [],
        "claims": [],
        "verified_claims": [],
        "rejected_claims": [],
        "agent_invocations": 0,
        "cost_usd": 0.0,
        "messages": [],
    }


async def _emit_cost_if_changed(session_id: UUID, state: ResearchGraphState) -> None:
    cost = float(state.get("cost_usd") or 0.0)
    inv = int(state.get("agent_invocations") or 0)
    await append_event(
        session_id,
        "cost_update",
        {
            "total_usd": cost,
            "invocations": inv,
        },
    )


async def _sync_session_progress_db(session_id: UUID, state: ResearchGraphState) -> None:
    """
    Refresh poll-friendly counters while the job runs.

    LangGraph only emits stream chunks after each node finishes, so during a long
    first LLM call ``agent_invocation_count`` stays 0 in both state and DB — that
    is expected. After each step completes, GET /research/{id} reflects progress.
    """
    cost = float(state.get("cost_usd") or 0.0)
    inv = int(state.get("agent_invocations") or 0)
    async with AsyncSessionLocal() as db:
        row = await db.get(ResearchSession, session_id)
        if row is None:
            return
        row.total_cost_usd = Decimal(str(cost)).quantize(Decimal("0.000001"))
        row.agent_invocation_count = inv
        await db.commit()


async def run_research_job(session_id: UUID, *, resume: bool = False) -> None:
    """
    Stream the compiled graph, persisting terminal accounting + JSON state.

    When ``resume=True``, LangGraph continues from the latest checkpoint for the
    same ``thread_id`` (session UUID). The row must already exist.
    """
    async with AsyncPostgresSaver.from_conn_string(_checkpoint_conn_string()) as checkpointer:
        await checkpointer.setup()
        graph = compile_research_graph(checkpointer)
        config: dict[str, Any] = {"configurable": {"thread_id": str(session_id)}}

        query_text: str | None = None
        async with AsyncSessionLocal() as db:
            row = await db.get(ResearchSession, session_id)
            if row is None:
                logger.error("Research session %s not found", session_id)
                return

            query_text = row.query
            if resume:
                row.error_message = None
            row.status = "running"
            await db.commit()

        if query_text is None:
            return

        if not resume:
            cfg = get_settings()
            await append_event(
                session_id,
                "run_config",
                {
                    "model_strong": cfg.model_strong,
                    "model_fast": cfg.model_fast,
                    "embedding_model": cfg.embedding_model,
                    "similarity_mode": cfg.similarity_mode,
                    "max_parallel_agent_calls": cfg.max_parallel_agent_calls,
                    "session_cost_limit_usd": str(cfg.session_cost_limit_usd),
                },
            )

        stream_input: Any = None if resume else _initial_state(session_id, query_text)
        final: ResearchGraphState | None = None
        last_cost_sig: tuple[float, int] | None = None

        try:
            try:
                async for state in graph.astream(stream_input, config, stream_mode="values"):
                    final = state
                    cost = float(state.get("cost_usd") or 0.0)
                    inv = int(state.get("agent_invocations") or 0)
                    sig = (round(cost, 6), inv)
                    if sig != last_cost_sig:
                        last_cost_sig = sig
                        await _emit_cost_if_changed(session_id, state)
                        await _sync_session_progress_db(session_id, state)
            except Exception as stream_exc:  # noqa: BLE001 — degrade gracefully
                logger.warning(
                    "Graph streaming failed; falling back to one-shot invoke: %s",
                    stream_exc,
                )
                final = await graph.ainvoke(stream_input, config)
                if final:
                    await _emit_cost_if_changed(session_id, final)
                    await _sync_session_progress_db(session_id, final)
        except Exception as exc:  # noqa: BLE001 — top-level runner captures all failures
            logger.exception("Research session %s failed", session_id)
            async with AsyncSessionLocal() as db:
                row = await db.get(ResearchSession, session_id)
                if row is not None:
                    row.status = "failed"
                    row.error_message = str(exc)
                    await db.commit()
            await append_event(
                session_id, "session_status", {"status": "failed", "error": str(exc)}
            )
            return

        async with AsyncSessionLocal() as db:
            row = await db.get(ResearchSession, session_id)
            if row is None:
                return
            if not final:
                row.status = "failed"
                row.error_message = "Graph produced no terminal state."
                await db.commit()
                return
            row.status = "completed"
            row.final_report = final.get("final_report")
            row.graph_state = json.loads(json.dumps(final, default=str))
            row.total_cost_usd = Decimal(str(final.get("cost_usd") or 0.0)).quantize(
                Decimal("0.000001")
            )
            row.agent_invocation_count = int(final.get("agent_invocations") or 0)
            row.error_message = None
            await db.commit()

        await append_event(session_id, "session_status", {"status": "completed"})


async def load_session(db: AsyncSession, session_id: UUID) -> ResearchSession | None:
    """Helper for API detail fetches."""
    return await db.get(ResearchSession, session_id)


async def list_recent_sessions(db: AsyncSession, limit: int = 20) -> list[ResearchSession]:
    """Optional listing for debugging / admin (not exposed in MVP routes)."""
    from sqlalchemy import select

    res = await db.execute(
        select(ResearchSession).order_by(ResearchSession.created_at.desc()).limit(limit)
    )
    return list(res.scalars().all())

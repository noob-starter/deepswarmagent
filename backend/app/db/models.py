"""
ORM models stored in the single PostgreSQL database.

``ResearchSession`` holds workflow status, serialized state snapshots for audit /
debugging, and the final report. LangGraph runs in-process; we persist outcomes
here for API polling and future frontend integration.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ResearchSession(Base):
    """
    One end-to-end research run from user query to final report.

    Attributes:
        id: Stable identifier returned by the API.
        query: Original user question.
        status: pending | running | completed | failed.
        graph_state: Optional JSON snapshot of the last LangGraph state.
        final_report: Markdown + inline trust annotations when completed.
        error_message: Populated when status is failed.
        total_cost_usd: Best-effort spend accumulator from LiteLLM metadata.
        agent_invocation_count: Rough counter for observability and caps.
    """

    __tablename__ = "research_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    query: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    graph_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    final_report: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6),
        nullable=False,
        default=Decimal("0"),
    )
    agent_invocation_count: Mapped[int] = mapped_column(nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    events: Mapped[list["ResearchEvent"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
    )


class ResearchEvent(Base):
    """
    Append-only SSE / audit log for a research session.

    ``event_type`` uses the event contract (e.g. ``agent_started``,
    ``tool_call``, ``agent_completed``, ``claim_verified``, ``cost_update``).
    ``payload`` is JSON for extensibility (agent ids, tool args summary, etc.).
    """

    __tablename__ = "research_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("research_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    session: Mapped["ResearchSession"] = relationship(back_populates="events")

"""research_events SSE / audit log table

Revision ID: 002_research_events
Revises: 001_create_research_sessions
Create Date: 2026-05-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002_research_events"
down_revision: Union[str, None] = "001_create_research_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "research_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["research_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_research_events_session_id", "research_events", ["session_id"])
    op.create_index("ix_research_events_event_type", "research_events", ["event_type"])


def downgrade() -> None:
    op.drop_index("ix_research_events_event_type", table_name="research_events")
    op.drop_index("ix_research_events_session_id", table_name="research_events")
    op.drop_table("research_events")

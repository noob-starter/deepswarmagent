"""API request/response models (stable JSON for future frontend)."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


class ResearchCreateRequest(BaseModel):
    """POST /api/v1/research body."""

    query: str = Field(..., min_length=3, max_length=16_000)


class ResearchSessionResponse(BaseModel):
    """Minimal session view for polling."""

    id: UUID
    query: str
    status: str
    total_cost_usd: Decimal
    agent_invocation_count: int
    created_at: datetime
    updated_at: datetime


class ResearchSessionDetailResponse(ResearchSessionResponse):
    """Includes outputs and optional debug state."""

    final_report: str | None = None
    error_message: str | None = None
    graph_state: dict | None = None

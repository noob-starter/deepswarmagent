"""
Structured types shared by LangGraph state, tools, and agents.

The graph state mirrors the architecture doc: plan, sub-questions, findings,
claims with trust scores, and cost accounting. Fields with ``Annotated`` use
reducers so parallel branches (future Send-based map-reduce) can append safely.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from typing_extensions import Required


class SourceDict(TypedDict, total=False):
    """Normalized citation/source record from any retrieval backend."""

    source_id: Required[str]
    url: str
    title: str
    snippet: str
    full_content: NotRequired[str | None]
    published_date: NotRequired[str | None]
    domain_authority: NotRequired[float]
    tool_name: NotRequired[str]


class SubQuestionDict(TypedDict, total=False):
    """One unit of work assigned to a Searcher worker."""

    id: Required[str]
    text: Required[str]
    status: Required[Literal["pending", "running", "done"]]
    assigned_tools: NotRequired[list[str]]
    findings: NotRequired[str | None]
    sources: NotRequired[list[SourceDict]]


class ClaimDict(TypedDict, total=False):
    """Substantive claim with provenance and trust metadata."""

    id: Required[str]
    claim: Required[str]
    source_ids: Required[list[str]]
    trust_score: NotRequired[int]
    trust_breakdown: NotRequired[dict[str, Any]]
    fact_check_notes: NotRequired[str]
    flags: NotRequired[list[str]]


def add_cost(left: float | None, right: float | None) -> float:
    """LangGraph reducer: sum incremental cost deltas (USD)."""
    return float(left or 0.0) + float(right or 0.0)


class ResearchGraphState(TypedDict, total=False):
    """
    LangGraph state for phases 0–3.

    Lists marked with ``operator.add`` accumulate partial updates across branches.
    Scalar fields are last-write-wins unless the node returns the full key anew.
    """

    session_id: Required[str]
    user_query: Required[str]

    plan: dict[str, Any]
    pending_sub_questions: list[dict[str, Any]]

    findings_summaries: Annotated[list[str], operator.add]
    all_sources: Annotated[list[SourceDict], operator.add]

    critic_round: int
    critic_followups: list[str]

    claims: list[ClaimDict]
    verified_claims: list[ClaimDict]
    rejected_claims: list[dict[str, Any]]

    draft_report: str
    final_report: str

    agent_invocations: Annotated[int, operator.add]
    cost_usd: Annotated[float, add_cost]
    messages: Annotated[list[str], operator.add]
    stop_reason: str

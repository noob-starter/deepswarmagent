"""
Assemble the LangGraph workflow for research.

``compile_research_graph`` attaches telemetry wrappers and accepts an optional
Postgres checkpointer for durable resume points between node transitions.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.graph.nodes.critic import critic_node
from app.graph.nodes.planner import planner_node
from app.graph.nodes.postprocess import (
    citation_formatter_node,
    extract_claims_node,
    fact_check_node,
    synthesizer_node,
)
from app.graph.nodes.router import after_critic, critic_route_prepare_node
from app.graph.nodes.search import parallel_search_node
from app.graph.telemetry import wrap_node
from app.schemas.state import ResearchGraphState


def compile_research_graph(checkpointer: Any | None = None):
    """Return a compiled graph, optionally backed by a LangGraph checkpointer."""
    builder = StateGraph(ResearchGraphState)
    builder.add_node("planner", wrap_node("planner", planner_node))
    builder.add_node("parallel_search", wrap_node("parallel_search", parallel_search_node))
    builder.add_node("critic", wrap_node("critic", critic_node))
    builder.add_node(
        "critic_route_prepare",
        wrap_node("critic_route_prepare", critic_route_prepare_node, label="Critic router"),
    )
    builder.add_node("extract_claims", wrap_node("extract_claims", extract_claims_node))
    builder.add_node("fact_check", wrap_node("fact_check", fact_check_node))
    builder.add_node("synthesize", wrap_node("synthesize", synthesizer_node))
    builder.add_node(
        "citation_format",
        wrap_node("citation_format", citation_formatter_node, label="Citation formatter"),
    )

    builder.add_edge(START, "planner")
    builder.add_edge("planner", "parallel_search")
    builder.add_edge("parallel_search", "critic")
    builder.add_conditional_edges(
        "critic",
        after_critic,
        {
            "search_again": "critic_route_prepare",
            "extract_claims": "extract_claims",
        },
    )
    builder.add_edge("critic_route_prepare", "parallel_search")
    builder.add_edge("extract_claims", "fact_check")
    builder.add_edge("fact_check", "synthesize")
    builder.add_edge("synthesize", "citation_format")
    builder.add_edge("citation_format", END)
    return builder.compile(checkpointer=checkpointer)

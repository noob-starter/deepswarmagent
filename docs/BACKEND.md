# Backend documentation

This section describes the **Deep Research Swarm** backend: the LangGraph research pipeline, state model, scoring math, tools, and how HTTP + Postgres fit together.

## Contents

| Document | What you will find |
|----------|-------------------|
| [backend/01-overview.md](backend/01-overview.md) | Stack, entrypoints, config knobs, how a session runs end-to-end |
| [backend/02-state-and-data-models.md](backend/02-state-and-data-models.md) | `ResearchGraphState`, Pydantic/API models, SQLAlchemy tables, reducers |
| [backend/03-langgraph-nodes.md](backend/03-langgraph-nodes.md) | Each graph node: inputs, outputs, LLM prompts, branching (critic loop) |
| [backend/04-scoring-trust-and-similarity.md](backend/04-scoring-trust-and-similarity.md) | Trust breakdown dimensions, weights, fact-check merge, citation alignment |
| [backend/05-tools-search-fetch.md](backend/05-tools-search-fetch.md) | Web / arXiv / GitHub-hint search, `SourceDict` normalization, HTTP fetch |
| [backend/06-api-persistence-and-runner.md](backend/06-api-persistence-and-runner.md) | REST routes, SSE events, checkpointer, `research_runner` streaming |

## Figure: research pipeline (SVG)

A static diagram of the compiled graph lives at [backend/figures/research-graph-flow.svg](backend/figures/research-graph-flow.svg) (open in browser or IDE preview).

## Related

- [ARCHITECTURE.md](ARCHITECTURE.md) — runtime topology, SSE contract, checkpoints, failure modes

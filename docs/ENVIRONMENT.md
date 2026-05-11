# Environment variables

Configuration is defined in `backend/app/config.py` (`Settings`) and loaded from the process environment (and optional `.env` files at the repo root or under `backend/`). Names below are the **environment variable** names (typically `UPPER_SNAKE_CASE`).

Never commit real secrets; use `.env` locally (gitignored) and your host’s secret store in production.

## Required

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | PostgreSQL URL for the API. Use `postgresql+asyncpg://…` or plain `postgresql://…` (the app rewrites the latter for asyncpg). **Supabase (recommended on IPv4-only hosts, e.g. Render):** use the **Session pooler** URI from the dashboard (`postgres.<project_ref>@aws-*-<region>.pooler.supabase.com:5432`). Direct `db.<ref>.supabase.co` is often IPv6-only publicly — only then set `DATABASE_SUPABASE_IPV4=true` or switch to the pooler URL. |

## API server

| Variable | Default | Description |
|----------|---------|-------------|
| `API_HOST` | `0.0.0.0` | Bind address. |
| `API_PORT` | `8000` | Port when not using platform `PORT` (e.g. local Docker). |
| `ENVIRONMENT` | `local` | `local` or `production`. In `production`, if `MODEL_STRONG` / `MODEL_FAST` are still the default Ollama ids, they are replaced with `gemini/gemini-2.5-flash`. |
| `CORS_ORIGINS` | Local Vite + Compose origins | Comma-separated allowed browser origins. Production: include your static frontend origin (e.g. `https://your-app.vercel.app`). |
| `DATABASE_SUPABASE_IPV4` | `false` | If `true` and `DATABASE_URL` uses Supabase **direct** `db.*.supabase.co`, resolve IPv4 or fall back to Session pooler (for IPv4-only PaaS). Leave `false` when `DATABASE_URL` is already a `*.pooler.supabase.com` URI (typical production). |
| `DATABASE_HOSTADDR` | — | Optional IPv4 for libpq `hostaddr` when using direct `db.*` with `DATABASE_SUPABASE_IPV4=true` and auto-resolution fails. |
| `SUPABASE_POOLER_REGION` | — | e.g. `ap-northeast-1` — only helps automatic pooler rewrite when `DATABASE_SUPABASE_IPV4=true` and direct host has no public IPv4. Omit with a pooler `DATABASE_URL`. |
| `PORT` | — | Set by platforms such as Render; entrypoint listens on `PORT` when present. |

## Database pool

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_POOL_SIZE` | `5` | SQLAlchemy pool size. |
| `DB_MAX_OVERFLOW` | `10` | Additional connections beyond the pool size. |

## LLM & embeddings (LiteLLM)

| Variable | Default | Description |
|----------|---------|-------------|
| `LITELLM_API_KEY` | — | Optional key for OpenAI-compatible providers. |
| `GOOGLE_API_KEY` / `GEMINI_API_KEY` | — | Google AI (Gemini). Alias keys are equivalent. |
| `OLLAMA_API_BASE` | — | e.g. `http://ollama:11434` (Compose) or `http://host.docker.internal:11434` (API container → host Ollama). |
| `MODEL_STRONG` | `ollama/llama3.2:3b` | Planner, critic, synthesizer, claim extraction. |
| `MODEL_FAST` | `ollama/llama3.2:1b` | Search summaries, verifier. |
| `EMBEDDING_MODEL` | `ollama/nomic-embed-text` | Used when `SIMILARITY_MODE=litellm`. |
| `SIMILARITY_MODE` | `tfidf` | `tfidf` (no embedding API) or `litellm`. |
| `LITELLM_NUM_RETRIES` | `3` | LiteLLM retry count. |
| `LITELLM_REQUEST_TIMEOUT` | `900` | Seconds (large values help slow local Ollama). |

## Search tools

| Variable | Default | Description |
|----------|---------|-------------|
| `TAVILY_API_KEY` | — | Optional Tavily search. |
| `SERPER_API_KEY` | — | Optional Serper. |
| `SEARCH_ARCHIVE_SUPPLEMENT` | `true` | Supplement with `web.archive.org` hints from DuckDuckGo. |

## Claim extraction tuning

| Variable | Default | Description |
|----------|---------|-------------|
| `EXTRACT_CLAIMS_TEMPERATURE` | `0.12` | Temperature for JSON claim extraction. |
| `EXTRACT_CLAIMS_MAX_TOKENS` | `2240` | Max tokens for that call. |

## Safety & graph limits

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_PARALLEL_AGENT_CALLS` | `10` | Concurrency cap. |
| `MAX_TOOL_CALLS_PER_AGENT_INVOCATION` | `8` | Per-invocation tool cap. |
| `MAX_CRITIC_ROUNDS` | `2` | Critic loop bound. |
| `MAX_SUB_QUESTIONS_PER_WAVE` | `8` | Sub-questions per wave. |
| `SESSION_COST_LIMIT_USD` | `0` | Stop when cumulative LiteLLM cost reaches this (0 = no cap). |
| `CITATION_SIMILARITY_THRESHOLD` | `0.7` | Citation / similarity threshold. |

## HTTP fetch (page text)

| Variable | Default | Description |
|----------|---------|-------------|
| `FETCH_MAX_BYTES` | `2000000` | Max response body size. |
| `FETCH_TIMEOUT_SECONDS` | `30` | Request timeout. |
| `USER_AGENT` | (see `config.py`) | Outbound `User-Agent` for fetches. |

## SSE (UI streams)

| Variable | Default | Description |
|----------|---------|-------------|
| `SSE_POLL_INTERVAL_SECONDS` | `0.35` | Poll interval for replaying events. |
| `SSE_REPLAY_MAX_EVENTS` | `500` | Max backlog events for reconnects. |

## Langfuse (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `LANGFUSE_PUBLIC_KEY` | — | Public key. |
| `LANGFUSE_SECRET_KEY` | — | Secret key. |
| `LANGFUSE_HOST` | — | e.g. `https://cloud.langfuse.com` or regional URL. |

## Frontend (Vite build only)

Set these in the **frontend** build environment (e.g. Vercel), not on the API:

| Variable | Description |
|----------|-------------|
| `VITE_API_BASE_URL` | Public URL of the API (no trailing slash), e.g. `https://your-api.onrender.com`. Empty in Docker Compose (same origin via nginx). |

See [DEPLOYMENT.md](DEPLOYMENT.md) for how these fit together in production.

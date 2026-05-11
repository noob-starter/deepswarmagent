# Deep Research Swarm (multi-agent + UI)

Monorepo layout:

| Path | Role |
|------|------|
| `backend/` | FastAPI + LangGraph + LiteLLM + Postgres (sessions, events, checkpoints) |
| `frontend/` | Vite + React + React Flow dashboard (SSE) |
| `eval/` | Sample eval JSONL + `run_eval.py` harness |
| `docs/` | Architecture, **[complete backend reference](docs/BACKEND.md)**, figures |

---

## LangGraph research workflow

The research pipeline is a **LangGraph** `StateGraph` compiled in [`backend/app/graph/build.py`](backend/app/graph/build.py): plan → parallel search → critic (may loop) → structured claims → fact-check → synthesize → citation formatting.

```mermaid
flowchart TD
  START([START]) --> planner
  planner --> parallel_search
  parallel_search --> critic
  critic -->|after_critic| route{Branch}
  route -->|search_again| critic_route_prepare
  route -->|extract_claims| extract_claims
  critic_route_prepare --> parallel_search
  extract_claims --> fact_check
  fact_check --> synthesize
  synthesize --> citation_format
  citation_format --> END([END])
```

Static SVG (printable): [docs/backend/figures/research-graph-flow.svg](docs/backend/figures/research-graph-flow.svg). Step-by-step node behavior: [docs/backend/03-langgraph-nodes.md](docs/backend/03-langgraph-nodes.md).

---

## Prerequisites

- **Docker Desktop** (or Docker Engine + Compose v2) for the full stack, **or**
- **Python 3.11–3.13** and **Node.js 20+** for local split development.
- **PostgreSQL 16** if you run the API on the host (Compose provides `db` otherwise).
- **Ollama**: Compose runs **`ollama`** as a service and routes the API to `http://ollama:11434`. Pull models once after the container exists (see below). Alternatively install [Ollama](https://ollama.com) on the host and point `.env` at `host.docker.internal`.

---

## One-time setup

From the repository root (`deepswarmagent/` or your clone path):

```bash
cp .env.example .env
```

Edit `.env`: set `DATABASE_URL`, optional cloud keys (`LITELLM_API_KEY`, …), optional `LANGFUSE_*`, `TAVILY_API_KEY`, etc. Environment variables are described in `backend/app/config.py`; a narrative guide lives in [docs/BACKEND.md](docs/BACKEND.md) and [docs/backend/01-overview.md](docs/backend/01-overview.md).

With Compose, **`OLLAMA_API_BASE` is overridden** for the `api` container to `http://ollama:11434`. After the first `docker compose up`, pull the models referenced by `MODEL_STRONG` / `MODEL_FAST` (defaults use **Llama 3.2** small tiers):

```bash
docker compose exec ollama ollama pull llama3.2:3b
docker compose exec ollama ollama pull llama3.2:1b
```

Ollama inside Docker on CPU can take **minutes** before the first token (cold load + inference). Keep **`LITELLM_REQUEST_TIMEOUT`** high enough (`.env.example` defaults to **900** seconds); if you still see timeouts, raise it further or switch models (defaults favor **light** `llama3.2:3b` / `llama3.2:1b`; set `MODEL_STRONG=ollama/llama3.1` for higher quality at the cost of speed).

---

## Commands — full stack (Docker Compose)

Build and start **database + API + web UI**:

```bash
docker compose build
docker compose up
```

Rebuild images after dependency or Dockerfile changes:

```bash
docker compose up --build
```

Run in the background:

```bash
docker compose up -d
```

View logs:

```bash
docker compose logs -f
docker compose logs -f api
docker compose logs -f web
docker compose logs -f db
docker compose logs -f ollama
```

Stop services:

```bash
docker compose down
```

Stop and remove the Postgres volume (wipes local data):

```bash
docker compose down -v
```

Custom ports (from `.env`):

- `API_PORT` — default `8000` (host → container).
- `WEB_PORT` — default `3000` (host → nginx).
- `OLLAMA_HOST_PORT` — default `11434` (host → Ollama HTTP API, optional).

---

## URLs (default ports)

| Service | URL |
|---------|-----|
| Web UI | http://localhost:3000 |
| API (direct) | http://localhost:8000 |
| Ollama API (host → Compose service) | http://localhost:11434 |
| Health (API) | http://localhost:8000/health |
| Health (via nginx) | http://localhost:3000/health |

---

## Commands — backend only (host / virtualenv)

Use this when Postgres is already running (e.g. `docker compose up db` only).

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install .
```

Export database URL (example for Compose `db` service on localhost):

```bash
export DATABASE_URL="postgresql+psycopg_async://research:research@127.0.0.1:5432/research_swarm"
```

Run Alembic migrations:

```bash
cd backend
source .venv/bin/activate
alembic upgrade head
```

Start the API with auto-reload:

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

---

## Commands — frontend only

**Development** (expects API at `http://127.0.0.1:8000`; Vite proxies `/api` and `/health`):

```bash
cd frontend
npm install
npm run dev
```

Then open **http://localhost:5173**.

**Production build** (static files in `frontend/dist/`):

```bash
cd frontend
npm install
npm run build
```

**Preview production build locally:**

```bash
cd frontend
npm run preview
```

---

## Commands — split dev (DB + API in Docker, UI on host)

```bash
docker compose up db api
```

In another terminal:

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 — API calls proxy to `localhost:8000`.

---

## Commands — evaluation harness

Requires the API reachable (e.g. Compose or local Uvicorn).

```bash
pip install httpx
export EVAL_API_BASE="http://127.0.0.1:8000"
python eval/run_eval.py --questions eval/questions.sample.jsonl --out eval/results.jsonl
```

See `eval/README.md` for methodology and extending the baseline.

---

## API quick checks (`curl`)

Create a research session:

```bash
curl -sS -X POST "http://localhost:8000/api/v1/research" \
  -H "Content-Type: application/json" \
  -d '{"query":"What is LangGraph used for?"}'
```

Save `id` from the response, then poll status and report:

```bash
curl -sS "http://localhost:8000/api/v1/research/SESSION_ID_HERE"
```

Server-Sent Events (replace `SESSION_ID_HERE`; reconnect with `&after_id=LAST_EVENT_ID`):

```bash
curl -sS -N "http://localhost:8000/api/v1/research/SESSION_ID_HERE/stream?after_id=0"
```

Resume from checkpoint (only if status is `failed` or `running`):

```bash
curl -sS -X POST "http://localhost:8000/api/v1/research/SESSION_ID_HERE/resume"
```

---

## API flow (reference)

1. `POST /api/v1/research` with `{"query":"..."}`
2. `GET /api/v1/research/{id}/stream` (SSE) for live events
3. `GET /api/v1/research/{id}` for the final Markdown report
4. `POST /api/v1/research/{id}/resume` to continue from the latest LangGraph Postgres checkpoint when needed

---

## More documentation

- `docs/DEPLOYMENT.md` — production, nginx, SSE, checkpoints  
- `docs/ENVIRONMENT.md` — all environment variables  
- `docs/ARCHITECTURE.md` — phases, diagrams  
- `docs/FOLDER_STRUCTURE.md` — package map  

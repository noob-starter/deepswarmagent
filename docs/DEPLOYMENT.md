# Production deployment

This monorepo is split into a **Python API** (Docker) and a **static React UI** (Vite build). The reference production layout is:

- **API**: container from `backend/Dockerfile` (repo root context), e.g. [Render](https://render.com) **Web Service** — see `render.yaml`.
- **Database**: managed PostgreSQL (e.g. [Neon](https://neon.tech)); set `DATABASE_URL` on the API.
- **UI**: static hosting (e.g. [Vercel](https://vercel.com)) from `frontend/` with `npm run build`.

Compose (`docker-compose.yml`) is intended for **local** full stack (Postgres + Ollama + API + nginx). Do not assume Ollama exists in cloud production unless you operate it yourself.

## API container behavior

- **Migrations**: `backend/scripts/docker-entrypoint.sh` runs `alembic upgrade head` before starting Uvicorn.
- **Port**: listens on `PORT` when set (Render), otherwise `API_PORT` / `8000`.
- **Health**: `GET /health` returns `200` with `{"status":"ok","database":"ok"}` when the app can reach Postgres; `503` with `database_unreachable` if not.

## Render (blueprint)

`render.yaml` declares a Docker web service. In the Render dashboard, set at least:

- `DATABASE_URL` — from Neon (or your provider).
- `GOOGLE_API_KEY` — if you rely on Gemini in production (see `ENVIRONMENT=production` and model defaults in `backend/app/config.py`).
- `CORS_ORIGINS` — comma-separated list including your **frontend** origin (e.g. `https://your-app.vercel.app`).

Add any other variables from [ENVIRONMENT.md](ENVIRONMENT.md) as needed (`TAVILY_API_KEY`, `LANGFUSE_*`, etc.).

## Frontend (Vercel or similar)

1. Connect the repo (or deploy from `frontend/`).
2. Build command: `npm run build` (install with `npm install` or `npm ci` if you commit a lockfile).
3. Output directory: `dist`.
4. Set **`VITE_API_BASE_URL`** to the public API URL (scheme + host, no path), e.g. `https://deep-research-swarm-api.onrender.com`.

The SPA calls `/api/v1/...` relative to that base. CORS on the API must allow the frontend origin.

## CORS and cookies

`CORS_ORIGINS` must list every browser origin that will talk to the API. The API enables `allow_credentials=True`; keep origins explicit (avoid `*` in production).

## SSE and reverse proxies

Long-lived Server-Sent Events (`GET /api/v1/research/{id}/stream`) need proxies to avoid short read timeouts. The bundled `frontend/nginx.conf` sets `proxy_read_timeout 3600s` for `/api/`. Configure the same class of timeouts on any edge proxy in front of the API in production.

## Secrets checklist

- [ ] `DATABASE_URL` on API host  
- [ ] `GOOGLE_API_KEY` (or other LLM keys) if not using self-hosted Ollama  
- [ ] `CORS_ORIGINS` matching the deployed UI URL  
- [ ] `VITE_API_BASE_URL` on the static host matching the API public URL  

See [ENVIRONMENT.md](ENVIRONMENT.md) for the full variable list.

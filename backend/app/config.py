"""
Application configuration loaded from environment variables.

All settings are documented in ``docs/ENVIRONMENT.md``. Use ``.env`` locally
and your platform's secret store in production—never commit real keys.
"""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlparse, urlunparse

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ``config.py`` lives in ``backend/app/`` — resolve env files so ``uvicorn`` from
# ``backend/`` still loads a monorepo-root ``.env`` (same layout as Docker Compose).
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BACKEND_ROOT.parent


def parse_cors_origins_list(raw: str) -> list[str]:
    """
    Split ``CORS_ORIGINS`` and normalize entries for CORSMiddleware matching.

    Browsers send ``Origin`` without a trailing slash; pasted URLs often include one.
    """
    items: list[str] = []
    for part in raw.split(","):
        o = part.strip()
        if not o:
            continue
        if (o.startswith('"') and o.endswith('"')) or (o.startswith("'") and o.endswith("'")):
            o = o[1:-1].strip()
        o = o.rstrip("/")
        if o:
            items.append(o)
    return items


def _discovered_env_files() -> tuple[str, ...]:
    candidates = (
        _REPO_ROOT / ".env",
        _REPO_ROOT / ".env.local",
        _BACKEND_ROOT / ".env",
        _BACKEND_ROOT / ".env.local",
        Path(".env"),
        Path(".env.local"),
    )
    return tuple(str(p.resolve()) for p in candidates if p.is_file())


def _strip_asyncpg_incompatible_query(url: str) -> str:
    """
    Neon connection strings often include ``channel_binding=require``.
    asyncpg does not implement SCRAM channel binding, so that parameter breaks connects.
    """
    parsed = urlparse(url)
    if not parsed.query:
        return url
    kept: list[str] = []
    for segment in parsed.query.split("&"):
        if not segment:
            continue
        key = segment.split("=", 1)[0].lower()
        if key == "channel_binding":
            continue
        kept.append(segment)
    new_query = "&".join(kept)
    return urlunparse(parsed._replace(query=new_query))


class Settings(BaseSettings):
    """Runtime configuration (12-factor friendly)."""

    model_config = SettingsConfigDict(
        env_file=_discovered_env_files() or None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- API ---
    api_host: str = Field(default="0.0.0.0", description="Bind address for uvicorn.")
    api_port: int = Field(default=8000, ge=1, le=65535)
    api_title: str = Field(default="Deep Research Swarm API")
    environment: Literal["local", "production"] = "local"
    cors_origins: str = Field(
        default="http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://127.0.0.1:5173",
        description="Comma-separated origins for CORS (vite dev + docker web).",
    )

    # --- Database (single DB: PostgreSQL only) ---
    database_url: str = Field(
        ...,
        description=(
            "Async SQLAlchemy URL, e.g. "
            "postgresql+asyncpg://user:pass@localhost:5432/research_swarm"
        ),
    )
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=10, ge=0, le=100)

    # --- LiteLLM / models (local: Ollama; production: Gemini via GOOGLE_API_KEY) ---
    litellm_api_key: str | None = Field(
        default=None,
        description="Optional API key for OpenAI-compatible providers.",
    )
    google_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        description="Google AI (Gemini) API key — required for gemini/* models in production.",
    )
    # When using Ollama locally, point LiteLLM at the host (Docker: host.docker.internal)
    ollama_api_base: str | None = Field(
        default=None,
        description="e.g. http://host.docker.internal:11434 for Docker → host Ollama.",
    )
    model_strong: str = Field(
        default="ollama/llama3.2:3b",
        description="Planner, critic, synthesizer, claim extraction (lighter default for CPU/Docker).",
    )
    model_fast: str = Field(
        default="ollama/llama3.2:1b",
        description="Search summaries + fact-check verifier — smallest default for latency.",
    )
    embedding_model: str = Field(
        default="ollama/nomic-embed-text",
        description="LiteLLM embedding model id, or set similarity_mode=tfidf.",
    )
    similarity_mode: Literal["litellm", "tfidf"] = Field(
        default="tfidf",
        description="tfidf needs no extra API; litellm uses embedding_model.",
    )

    # --- Search (free / optional keys) ---
    tavily_api_key: str | None = None
    serper_api_key: str | None = None
    search_archive_supplement: bool = Field(
        default=True,
        description="Append DuckDuckGo hits for web.archive.org to diversify toward archived URLs.",
    )

    # --- Claim extraction (model often drops JSON on tiny local models; tune here) ---
    extract_claims_temperature: float = Field(default=0.12, ge=0.0, le=1.0)
    extract_claims_max_tokens: int = Field(default=2240, ge=256, le=8192)

    # --- Safety & cost caps ---
    max_parallel_agent_calls: int = Field(default=10, ge=1, le=64)
    max_tool_calls_per_agent_invocation: int = Field(default=8, ge=1, le=32)
    max_critic_rounds: int = Field(default=2, ge=0, le=5)
    max_sub_questions_per_wave: int = Field(default=8, ge=1, le=20)
    session_cost_limit_usd: Decimal = Field(
        default=Decimal("0.00"),
        description="Stop graph when cost_usd reaches this (LiteLLM-reported). 0 = no cap.",
    )
    citation_similarity_threshold: float = Field(default=0.7, ge=0.0, le=1.0)

    # --- HTTP fetch for “browser-less” deep fetch (lite) ---
    fetch_max_bytes: int = Field(default=2_000_000, ge=10_000)
    fetch_timeout_seconds: float = Field(default=30.0, ge=1.0)
    user_agent: str = Field(
        default="DeepResearchSwarmBot/0.1 (+https://example.local; research)",
    )

    # --- SSE / UI ---
    sse_poll_interval_seconds: float = Field(
        default=0.35,
        ge=0.05,
        le=5.0,
        description="Polling interval while streaming research_events over SSE.",
    )
    sse_replay_max_events: int = Field(
        default=500,
        ge=10,
        le=10_000,
        description="Maximum backlog replay window for reconnects (newest N rows).",
    )

    # --- Langfuse (optional; https://langfuse.com free Cloud tier) ---
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str | None = Field(
        default=None,
        description="e.g. https://cloud.langfuse.com (optional for self-host).",
    )

    # --- LiteLLM resilience ---
    litellm_num_retries: int = Field(default=3, ge=0, le=12)
    # Local Ollama (especially in Docker / CPU) can take minutes on cold start + long JSON outputs.
    litellm_request_timeout: float = Field(default=900.0, ge=5.0, le=7200.0)

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_async_database_url(cls, v: object) -> object:
        """Allow Neon / platform `postgresql://` URIs; the API uses asyncpg."""
        if not isinstance(v, str):
            return v
        s = v.strip()
        # Render / dashboard paste sometimes wraps the whole URI in quotes.
        if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
            s = s[1:-1].strip()
        if s.startswith("postgresql://") and not s.startswith("postgresql+asyncpg://"):
            s = f"postgresql+asyncpg://{s[len('postgresql://') :]}"
        return _strip_asyncpg_incompatible_query(s)

    @model_validator(mode="after")
    def production_gemini_defaults(self) -> Self:
        """
        In production, default to Gemini 2.5 Flash when model env vars were left at
        local Ollama defaults. Explicit MODEL_STRONG / MODEL_FAST always win.
        """
        if self.environment != "production":
            return self
        prod = "gemini/gemini-2.5-flash"
        if self.model_strong == "ollama/llama3.2:3b":
            self.model_strong = prod
        if self.model_fast == "ollama/llama3.2:1b":
            self.model_fast = prod
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton for import-time access."""
    return Settings()


def reset_settings_cache() -> None:
    """Test helper: clear lru_cache for settings."""
    get_settings.cache_clear()

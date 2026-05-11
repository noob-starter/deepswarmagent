"""
FastAPI application entry — API + SSE + Langfuse hooks.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.routes import router
from app.config import get_settings
from app.db.session import AsyncSessionLocal
from app.services.observability import (
    configure_litellm_runtime,
    flush_langfuse_on_shutdown,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Wire LiteLLM retries + Langfuse once per worker process."""
    configure_litellm_runtime()
    try:
        yield
    finally:
        flush_langfuse_on_shutdown()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.api_title, version="0.2.0", lifespan=lifespan)

    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if not origins:
        origins = ["*"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def root_health() -> dict[str, str]:
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:
            logger.warning("Database health check failed: %s", exc)
            raise HTTPException(
                status_code=503,
                detail="database_unreachable",
            ) from exc
        return {"status": "ok", "database": "ok"}

    app.include_router(router)
    return app


app = create_app()

"""
FastAPI application entry — API + SSE + Langfuse hooks.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.routes import router
from app.config import get_settings, parse_cors_origins_list
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

    origins = parse_cors_origins_list(settings.cors_origins)
    if not origins:
        origins = ["*"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(SQLAlchemyError)
    async def _sqlalchemy_exception_handler(
        request: Request, exc: SQLAlchemyError
    ) -> JSONResponse:
        """
        Return a handled 503 so CORS headers are applied; unhandled DB errors
        become generic 500s from Starlette and browsers report a CORS failure.
        """
        logger.warning("Database error on %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=503,
            content={"detail": "database_error"},
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

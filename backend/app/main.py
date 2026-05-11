"""
FastAPI application entry — API + SSE + Langfuse hooks.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
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
    settings = get_settings()
    if settings.environment == "production":
        origins = parse_cors_origins_list(settings.cors_origins)
        if not any(o.startswith("https://") for o in origins):
            logger.warning(
                "Production CORS_ORIGINS has no https:// origin — browsers (e.g. Vercel) "
                "will block API calls unless you add your site, e.g. "
                "CORS_ORIGINS=https://deepswarmagent.vercel.app"
            )
    dbp = urlparse(settings.database_url)
    logger.info(
        "Startup: DATABASE_URL host=%s port=%s",
        dbp.hostname,
        dbp.port or 5432,
    )
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
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(
        request: Request, exc: HTTPException
    ) -> JSONResponse:
        """JSONResponse so error bodies stay consistent; CORS middleware still applies."""
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": jsonable_encoder(exc.detail)},
            headers=exc.headers or {},
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
            logger.warning(
                "Database health check failed (%s): %s",
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            raise HTTPException(
                status_code=503,
                detail="database_unreachable",
            ) from exc
        return {"status": "ok", "database": "ok"}

    app.include_router(router)
    return app


app = create_app()

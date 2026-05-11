"""
LiteLLM + Langfuse wiring.

Langfuse remains **optional** — without keys we only apply retry/timeouts.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import litellm

from app.config import get_settings

logger = logging.getLogger(__name__)


def litellm_langfuse_metadata(
    session_id: str | None,
    *,
    generation_name: str | None = None,
) -> dict[str, Any]:
    """
    Extra kwargs for LiteLLM ``acompletion`` / ``aembedding`` so Langfuse receives
    ``session_id`` (research session UUID) and optional ``generation_name``.

    No-ops when Langfuse keys are unset. ``generation_name`` alone is forwarded when keys
    are set (helps name spans even if ``session_id`` was omitted).
    """
    s = get_settings()
    pk = (s.langfuse_public_key or "").strip()
    sk = (s.langfuse_secret_key or "").strip()
    if not (pk and sk):
        return {}
    meta: dict[str, Any] = {}
    if session_id:
        meta["session_id"] = str(session_id)
    if generation_name:
        meta["generation_name"] = generation_name
    return meta


def _litellm_langfuse_callback_present() -> bool:
    chains = (
        litellm.success_callback
        + litellm.failure_callback
        + litellm._async_success_callback
        + litellm._async_failure_callback
    )
    for cb in chains:
        if cb == "langfuse":
            return True
        cls = getattr(cb, "__class__", type(None))
        if cls is not None and cls.__name__ == "LangFuseLogger":
            return True
    return False


def configure_litellm_runtime() -> None:
    """
    Idempotent process-level LiteLLM configuration.

    Call once during application startup (see ``main.create_app`` lifespan).
    """
    settings = get_settings()
    retries = settings.litellm_num_retries
    # Retrying slow local inference mostly stacks redundant wall-clock on timeouts.
    if settings.ollama_api_base:
        retries = min(retries, 1)
    litellm.num_retries = retries
    litellm.request_timeout = settings.litellm_request_timeout

    pk = (settings.langfuse_public_key or "").strip()
    sk = (settings.langfuse_secret_key or "").strip()
    if pk and sk:
        try:
            import langfuse  # noqa: F401 - LiteLLM Langfuse callback requires this package
        except ImportError:
            logger.error(
                "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are set but the `langfuse` package "
                "is not installed. Tracing will not work. Install with: pip install langfuse"
            )
            return
        if os.getenv("LANGFUSE_MOCK", "").lower() in ("1", "true", "yes"):
            logger.warning(
                "LANGFUSE_MOCK is set — LiteLLM will not send traces to Langfuse Cloud. "
                "Unset LANGFUSE_MOCK to record real traces."
            )
        # LiteLLM’s Langfuse client reads os.environ; overwrite empty placeholders.
        os.environ["LANGFUSE_PUBLIC_KEY"] = pk
        os.environ["LANGFUSE_SECRET_KEY"] = sk
        if settings.langfuse_host:
            os.environ["LANGFUSE_HOST"] = settings.langfuse_host.strip().rstrip("/")
        elif "LANGFUSE_HOST" not in os.environ:
            os.environ.setdefault("LANGFUSE_HOST", "https://cloud.langfuse.com")

        if not _litellm_langfuse_callback_present():
            litellm.logging_callback_manager.add_litellm_success_callback("langfuse")
            litellm.logging_callback_manager.add_litellm_failure_callback("langfuse")
        hf = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
        logger.info("Langfuse callbacks registered for LiteLLM (host=%s).", hf)
    else:
        logger.info("Langfuse keys not set — LiteLLM runs without external tracing.")


def flush_langfuse_on_shutdown() -> None:
    """Best-effort Langfuse SDK flush before process exit (batches telemetry)."""
    try:
        for cb in litellm.success_callback + litellm._async_success_callback:
            client = getattr(cb, "Langfuse", None)
            if client is not None and hasattr(client, "flush"):
                client.flush()
    except Exception:
        logger.debug("Langfuse flush on shutdown skipped.", exc_info=True)

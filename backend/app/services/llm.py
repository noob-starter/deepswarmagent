"""
LiteLLM-backed chat completions with concurrency and cost hints.

Designed for researcher-friendly setups:
- **Ollama** (local, free): set ``OLLAMA_API_BASE`` and models like ``ollama/llama3.2:3b``.
- **Cloud**: set ``LITELLM_API_KEY`` and e.g. ``openai/gpt-4o-mini``.

LiteLLM normalizes many providers: https://docs.litellm.ai/docs/providers
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import litellm
from litellm import acompletion

from app.config import get_settings
from app.services.observability import litellm_langfuse_metadata

# Reduce LiteLLM verbosity in local development
litellm.suppress_debug_info = True

_semaphore: asyncio.Semaphore | None = None


def _sem() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(get_settings().max_parallel_agent_calls)
    return _semaphore


def _litellm_extra(*, model: str) -> dict[str, Any]:
    """Provider connection options: Ollama base URL and/or API keys for cloud models."""
    s = get_settings()
    out: dict[str, Any] = {}
    if s.ollama_api_base:
        out["api_base"] = s.ollama_api_base
    ml = str(model).lower()
    if "gemini" in ml:
        g = (s.google_api_key or "").strip()
        if g:
            out["api_key"] = g
    elif s.litellm_api_key:
        out["api_key"] = str(s.litellm_api_key).strip()
    return out


def _extract_cost_usd(response: Any) -> float:
    """Best-effort spend from LiteLLM response object."""
    try:
        if hasattr(response, "_hidden_params") and isinstance(response._hidden_params, dict):
            v = response._hidden_params.get("response_cost")
            if v is not None:
                return float(v)
    except (TypeError, ValueError, AttributeError):
        pass
    return 0.0


def _use_openai_style_json_object(model: str) -> bool:
    """
    Only some stacks reliably honor ``response_format``.

    Ollama (and LM Studio) typically reject or stall on ``json_object`` mode,
    forcing LiteLLM down a wasteful double-call path.
    """
    m = str(model).lower().strip()
    return not m.startswith(("ollama/", "lmstudio/"))


async def chat_json(
    *,
    model: str,
    system: str,
    user: str,
    temperature: float = 0.12,
    top_p: float = 0.9,
    max_tokens: int | None = 2048,
    session_id: str | None = None,
    generation_name: str | None = None,
) -> tuple[dict[str, Any] | list[Any], float]:
    """
    Ask the model for **JSON-only** output; parse robustly.

    Returns:
        Parsed JSON object/list and estimated cost in USD.
    """
    async with _sem():
        s = get_settings()
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "request_timeout": s.litellm_request_timeout,
            **_litellm_extra(model=model),
        }
        fuse = litellm_langfuse_metadata(session_id, generation_name=generation_name)
        if fuse:
            kwargs["metadata"] = fuse
        if _use_openai_style_json_object(model):
            try:
                resp = await acompletion(**kwargs, response_format={"type": "json_object"})
            except Exception:
                resp = await acompletion(**kwargs)
        else:
            resp = await acompletion(**kwargs)
    cost = _extract_cost_usd(resp)
    content = resp.choices[0].message.content or "{}"
    # Ollama sometimes ignores json_object; strip fences.
    content = content.strip()
    content = re.sub(r"^```(?:json)?\s*", "", content)
    content = re.sub(r"\s*```$", "", content)
    try:
        parsed: Any = json.loads(content)
    except json.JSONDecodeError:
        parsed = {"_raw": content, "_parse_error": True}
    if isinstance(parsed, dict):
        return parsed, cost
    if isinstance(parsed, list):
        return parsed, cost
    return {"value": parsed}, cost


async def chat_text(
    *,
    model: str,
    system: str,
    user: str,
    temperature: float = 0.28,
    top_p: float = 0.92,
    max_tokens: int | None = 4096,
    session_id: str | None = None,
    generation_name: str | None = None,
) -> tuple[str, float]:
    """Standard chat completion returning assistant text."""
    async with _sem():
        s = get_settings()
        extra: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "request_timeout": s.litellm_request_timeout,
            **_litellm_extra(model=model),
        }
        fuse = litellm_langfuse_metadata(session_id, generation_name=generation_name)
        if fuse:
            extra["metadata"] = fuse
        resp = await acompletion(**extra)
    cost = _extract_cost_usd(resp)
    text = resp.choices[0].message.content or ""
    return text.strip(), cost

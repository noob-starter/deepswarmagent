"""
Claim–source alignment scoring for the citation formatter.

Modes:
- **tfidf** (default): scikit-learn TF-IDF + cosine similarity — no API keys.
- **litellm**: embedding models via LiteLLM (e.g. Ollama ``nomic-embed-text``).

Threshold comes from ``Settings.citation_similarity_threshold`` (default 0.7).
"""

from __future__ import annotations

from typing import Any

import numpy as np
from litellm import aembedding
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app.config import get_settings
from app.services.observability import litellm_langfuse_metadata


async def similarity_score(
    text_a: str,
    text_b: str,
    *,
    session_id: str | None = None,
    generation_name: str | None = "citation_similarity",
) -> float:
    """
    Return a cosine-like score in [0, 1] between two short texts.

    Falls back safely to 0.0 on empty input or backend errors.
    """
    settings = get_settings()
    if not text_a.strip() or not text_b.strip():
        return 0.0
    if settings.similarity_mode == "tfidf":
        return _tfidf_cosine(text_a, text_b)
    return await _embedding_cosine(
        text_a, text_b, session_id=session_id, generation_name=generation_name
    )


def _tfidf_cosine(a: str, b: str) -> float:
    """Unsupervised similarity — good enough to flag egregious citation mismatch."""
    vec = TfidfVectorizer(max_features=256, stop_words="english")
    try:
        matrix = vec.fit_transform([a, b])
        sim = cosine_similarity(matrix[0], matrix[1])[0][0]
        return float(max(0.0, min(1.0, sim)))
    except ValueError:
        return 0.0


async def _embedding_cosine(
    a: str,
    b: str,
    *,
    session_id: str | None,
    generation_name: str | None,
) -> float:
    """Use LiteLLM embeddings when configured (Ollama, OpenAI, etc.)."""
    settings = get_settings()
    kwargs = _embedding_connection_kwargs()
    fuse = litellm_langfuse_metadata(session_id, generation_name=generation_name)
    if fuse:
        kwargs["metadata"] = fuse
    try:
        resp = await aembedding(
            model=settings.embedding_model,
            input=[a, b],
            request_timeout=settings.litellm_request_timeout,
            **kwargs,
        )
        data = None
        if isinstance(resp, dict):
            data = resp.get("data")
        else:
            data = getattr(resp, "data", None)
        if not data or len(data) < 2:
            return _tfidf_cosine(a, b)
        v1 = np.array(data[0]["embedding"], dtype=np.float64)
        v2 = np.array(data[1]["embedding"], dtype=np.float64)
        denom = float(np.linalg.norm(v1) * np.linalg.norm(v2))
        if denom == 0.0:
            return 0.0
        sim = float(np.dot(v1, v2) / denom)
        return float(max(0.0, min(1.0, sim)))
    except Exception:
        return _tfidf_cosine(a, b)


def _embedding_connection_kwargs() -> dict[str, Any]:
    settings = get_settings()
    kwargs: dict[str, Any] = {}
    if settings.ollama_api_base:
        kwargs["api_base"] = settings.ollama_api_base
    em = str(settings.embedding_model).lower()
    if "gemini" in em:
        g = (settings.google_api_key or "").strip()
        if g:
            kwargs["api_key"] = g
    elif settings.litellm_api_key:
        kwargs["api_key"] = str(settings.litellm_api_key).strip()
    return kwargs

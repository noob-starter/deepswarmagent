#!/usr/bin/env python3
"""
Minimal eval driver — POST questions to the swarm API and record outcomes.

Extend `_single_baseline_stub` to call OpenAI / Ollama / another HTTP service.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE = os.environ.get("EVAL_API_BASE", "http://127.0.0.1:8000")


async def _post_research(client: httpx.AsyncClient, base: str, question: str) -> dict[str, Any]:
    r = await client.post(f"{base}/api/v1/research", json={"query": question}, timeout=60.0)
    r.raise_for_status()
    return r.json()


async def _wait_session(client: httpx.AsyncClient, base: str, session_id: str) -> dict[str, Any]:
    """Poll until terminal state or timeout."""
    deadline = time.time() + 900
    while time.time() < deadline:
        r = await client.get(f"{base}/api/v1/research/{session_id}", timeout=60.0)
        r.raise_for_status()
        row = r.json()
        if row.get("status") in {"completed", "failed"}:
            return row
        await asyncio.sleep(3)
    raise TimeoutError(session_id)


def _single_baseline_stub(question: str) -> str:
    """Replace with a real single-pass LLM call for A/B testing."""
    return f"[baseline stub] Echo: {question[:180]}"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--api-base", default=DEFAULT_BASE)
    args = parser.parse_args()

    lines = [ln for ln in args.questions.read_text().splitlines() if ln.strip()]
    args.out.parent.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient() as client:
        with args.out.open("w", encoding="utf-8") as out_f:
            for ln in lines:
                row = json.loads(ln)
                q = row["question"]
                session = await _post_research(client, args.api_base, q)
                sid = session["id"]
                final = await _wait_session(client, args.api_base, sid)
                record = {
                    "id": row.get("id"),
                    "question": q,
                    "swarm_session_id": sid,
                    "swarm_status": final.get("status"),
                    "swarm_report_excerpt": (final.get("final_report") or "")[:2000],
                    "baseline_answer": _single_baseline_stub(q),
                }
                out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
                print("done:", row.get("id"), final.get("status"))


if __name__ == "__main__":
    asyncio.run(main())

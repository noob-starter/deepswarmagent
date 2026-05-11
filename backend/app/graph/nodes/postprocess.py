"""
Claim extraction + fact checking + synthesis + citation formatting.

These nodes implement structured claims,
independent verification, trust scoring, and citation alignment checks.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.graph.nodes._util import budget_exceeded, truncate_chars, wrap_untrusted
from app.schemas.state import ClaimDict, ResearchGraphState, SourceDict
from app.services.embeddings import similarity_score
from app.services.llm import chat_json, chat_text
from app.services.session_events import append_event
from app.services.trust import (
    attach_trust_to_claim,
    domain_authority_for_url,
    is_internet_archive_snapshot_url,
)
from app.tools.search import unified_web_search

logger = logging.getLogger(__name__)


def _source_catalog(state: ResearchGraphState) -> tuple[dict[str, SourceDict], str]:
    cat: dict[str, SourceDict] = {}
    for s in state.get("all_sources") or []:
        sid = s.get("source_id")
        if sid:
            cat[sid] = s
    lines = [f"- {sid}: {s.get('title')} — {s.get('url')}" for sid, s in cat.items()]
    return cat, "\n".join(lines[:200])


def _markdown_link_label(title: str) -> str:
    t = title.replace("[", "").replace("]", "").replace("\n", " ").strip()
    return (t[:137] + "…") if len(t) > 140 else (t or "Source")


def _expand_catalog_citations(text: str, catalog: dict[str, SourceDict]) -> str:
    """Replace bracketed [source_id] with Markdown links for ids in catalog (longest id first)."""
    for sid in sorted(catalog.keys(), key=len, reverse=True):
        token = f"[{sid}]"
        if token not in text:
            continue
        src = catalog[sid]
        url = str(src.get("url") or "").strip()
        label = _markdown_link_label(str(src.get("title") or "Source"))
        repl = f"[{label}]({url})" if url else f"[{label}]"
        text = text.replace(token, repl)
    return text


def _strip_placeholder_numeric_citations(text: str, catalog: dict[str, SourceDict]) -> str:
    """
    Drop bracket cites like [src_3] when that id is not in the catalog (ordinal hallucinations).
    Leaves unknown non-numeric cites intact so typos remain visible for debugging.
    """

    def repl(m: re.Match[str]) -> str:
        sid = m.group(1)
        if sid in catalog:
            return m.group(0)
        if re.fullmatch(r"src_\d{1,3}", sid):
            return ""
        return m.group(0)

    return re.sub(r"\[(src_[^\]]+)\]", repl, text)


def _ranked_catalog_source_ids(catalog: dict[str, SourceDict]) -> list[str]:
    """Prefer Internet Archive URLs, then strongest domain tier (aligned with trust scoring)."""

    def sort_key(sid: str) -> tuple[int, float]:
        url = str(catalog[sid].get("url") or "")
        archive_first = 0 if is_internet_archive_snapshot_url(url) else 1
        auth = domain_authority_for_url(url)
        return (archive_first, -auth)

    return sorted(catalog.keys(), key=sort_key)


def _ensure_claims_cite_catalog(claims: list[ClaimDict], catalog: dict[str, SourceDict]) -> None:
    """Small models often emit claims with empty source_ids — anchor to catalog or trust pipeline starves."""
    if not catalog or not claims:
        return
    ranked = _ranked_catalog_source_ids(catalog)
    anchor = ranked[0]
    for c in claims:
        ids = [str(x) for x in (c.get("source_ids") or []) if str(x) in catalog]
        if ids:
            c["source_ids"] = ids
        else:
            c["source_ids"] = [anchor]


def _fallback_claims_from_catalog(
    catalog: dict[str, SourceDict],
    user_query: str,
) -> list[ClaimDict]:
    """Guarantee at least one citeable row when retrieval succeeded but JSON extraction failed."""
    if not catalog:
        return []
    out: list[ClaimDict] = []
    top = _ranked_catalog_source_ids(catalog)
    sid = top[0]
    src = catalog[sid]
    snip = str(src.get("snippet") or "").strip().replace("\n", " ")
    title = str(src.get("title") or "").strip()
    url = str(src.get("url") or "").strip()
    if len(snip) < 40:
        snip = title or url or "(no excerpt)"
    excerpt = truncate_chars(snip, 480)
    out.append(
        {
            "id": str(uuid.uuid4()),
            "claim": (
                f"Research note tied to retrieved source [{sid}] for «{truncate_chars(user_query, 140)}»: {excerpt}"
            ),
            "source_ids": [sid],
        },
    )
    return out


async def extract_claims_node(state: ResearchGraphState) -> dict[str, Any]:
    """Turn findings + cataloged sources into structured claims JSON."""
    if budget_exceeded(state):
        return {"claims": [], "messages": ["Budget exceeded before claim extraction."]}

    settings = get_settings()
    catalog, catalog_txt = _source_catalog(state)
    findings_raw = "\n\n".join(state.get("findings_summaries") or [])
    findings = truncate_chars(findings_raw, 18_000)
    system = (
        "Extract substantive factual claims for the user's question. "
        "Output JSON {claims: [{id, claim, source_ids: []}]} only.\n"
        "Rules:\n"
        "- Every claim MUST cite at least one source_id copied exactly from the Source catalog keys; never invent IDs.\n"
        "- If the catalog is non-empty you MUST emit at least one claim (minimum one row in `claims`).\n"
        "- Prefer citing high-trust URLs when relevance matches: federal/state .gov/.mil, .edu, Wikipedia, "
        "arxiv.org/github.com, Internet Archive snapshots (web.archive.org), canonical standards bodies.\n"
        "- Each claim text should stand alone as a single checkable factual sentence.\n"
    )
    user = (
        f"User query: {state['user_query']}\n\n"
        f"Source catalog:\n{catalog_txt or '(empty)'}\n\n"
        f"Findings:\n{wrap_untrusted(findings)}"
    )
    data, cost = await chat_json(
        model=settings.model_strong,
        system=system,
        user=user,
        temperature=float(settings.extract_claims_temperature),
        top_p=0.9,
        max_tokens=int(settings.extract_claims_max_tokens),
        session_id=str(state["session_id"]),
        generation_name="extract_claims",
    )

    claims: list[ClaimDict] = []
    if isinstance(data, dict):
        raw_claims = data.get("claims") or []
        if isinstance(raw_claims, list):
            for c in raw_claims[:30]:
                if not isinstance(c, dict):
                    continue
                text = str(c.get("claim") or "").strip()
                if not text:
                    continue
                sid_list = c.get("source_ids") or []
                if not isinstance(sid_list, list):
                    sid_list = []
                valid_ids = [str(x) for x in sid_list if str(x) in catalog]
                claims.append(
                    {
                        "id": str(c.get("id") or uuid.uuid4()),
                        "claim": text,
                        "source_ids": valid_ids,
                    }
                )

    _ensure_claims_cite_catalog(claims, catalog)
    if not claims and catalog:
        claims = _fallback_claims_from_catalog(catalog, str(state.get("user_query") or ""))
        if claims:
            logger.warning(
                "extract_claims: model returned no usable claims; using catalog fallback (%s sources).",
                len(catalog),
            )

    return {
        "claims": claims,
        "agent_invocations": 1,
        "cost_usd": cost,
        "messages": [f"Extracted {len(claims)} claims."],
    }


async def _verify_one_claim(
    claim: ClaimDict,
    session_id: str,
    sem: asyncio.Semaphore,
    settings: Any,
) -> tuple[ClaimDict, float, float, int]:
    """
    Fact-check a claim via an independent web pull + verifier LLM.

    Returns:
        claim enriched with notes, verifier cost, trust scalar 0-100, invocations
    """
    text = claim.get("claim") or ""
    inv = 0
    async with sem:
        alt_query = f"verify: {text}"
        sources = await unified_web_search(alt_query, session_id)
        inv += 1
        lines = [f"- {s.get('title')}: {s.get('snippet')[:400]}" for s in sources[:5]]
        block = "\n".join(lines) or "(no results)"
        system = (
            "You independently verify a claim versus fresh search snippets (untrusted). "
            "Output JSON with keys supports (boolean), score (number 0-100), notes (string)."
        )
        user = f"Claim: {text}\nSnippets:\n{wrap_untrusted(block)}"
        data, cost = await chat_json(
            model=settings.model_fast,
            system=system,
            user=user,
            temperature=0.05,
            top_p=0.85,
            max_tokens=640,
            session_id=session_id,
            generation_name="fact_check_claim",
        )
        inv += 1

    score = 50.0
    notes = ""
    if isinstance(data, dict):
        try:
            score = float(data.get("score", 50))
        except (TypeError, ValueError):
            score = 50.0
        notes = str(data.get("notes") or "")

    enriched: ClaimDict = {
        **claim,
        "fact_check_notes": notes,
    }
    return enriched, float(cost), float(score), inv


async def fact_check_node(state: ResearchGraphState) -> dict[str, Any]:
    """Parallel fact-check workers bound by the global semaphore settings."""
    if budget_exceeded(state):
        return {
            "verified_claims": list(state.get("claims") or []),
            "messages": ["Budget skip fact-check."],
        }

    settings = get_settings()
    claims = list(state.get("claims") or [])
    if not claims:
        return {"verified_claims": [], "messages": ["No claims to verify."]}

    catalog, _ = _source_catalog(state)
    sem = asyncio.Semaphore(settings.max_parallel_agent_calls)
    tasks = [_verify_one_claim(c, state["session_id"], sem, settings) for c in claims]
    rows = await asyncio.gather(*tasks)

    verified: list[ClaimDict] = []
    total_cost = 0.0
    total_inv = 0
    for claim, cst, fc_score, inv in rows:
        total_cost += cst
        total_inv += inv
        ver = attach_trust_to_claim(claim, catalog, fc_score)
        verified.append(ver)
        try:
            await append_event(
                UUID(state["session_id"]),
                "claim_verified",
                {
                    "claim_id": ver.get("id"),
                    "trust_score": ver.get("trust_score"),
                    "trust_breakdown": ver.get("trust_breakdown") or {},
                    "claim": (ver.get("claim") or "")[:400],
                    "source_ids": ver.get("source_ids") or [],
                    "sources": [
                        {
                            "source_id": sid,
                            "url": (catalog.get(sid) or {}).get("url") or "",
                            "title": ((catalog.get(sid) or {}).get("title") or "")[:300],
                        }
                        for sid in (ver.get("source_ids") or [])
                        if sid in catalog
                    ],
                },
            )
        except Exception as exc:
            logger.warning("append_event claim_verified failed: %s", exc)

    return {
        "verified_claims": verified,
        "agent_invocations": total_inv,
        "cost_usd": total_cost,
        "messages": [f"Fact-checked {len(verified)} claims."],
    }


async def synthesizer_node(state: ResearchGraphState) -> dict[str, Any]:
    """Compose a narrative report from verified claims + trust metadata only."""
    if budget_exceeded(state):
        return {"draft_report": "Stopped early due to budget cap.", "stop_reason": "budget"}

    settings = get_settings()
    chunks = []
    for c in state.get("verified_claims") or []:
        trust = int(c.get("trust_score") or 0)
        flag = "HIGH" if trust >= 81 else "MODERATE" if trust >= 51 else "LOW"
        chunks.append(
            f"- ({flag} {trust}/100) {c.get('claim')} | sources: {','.join(c.get('source_ids') or [])}"
        )
    block = "\n".join(chunks) or "(no verified claims)"
    catalog, catalog_txt = _source_catalog(state)
    system = (
        "Write a polished Markdown research brief with sections in this exact order:\n"
        "## Answer to your question\n"
        "Directly answer the user's exact question (named products, APIs, trade-offs, comparisons) in dense, authoritative prose. "
        "Use the evidence below as facts to cite; never discuss the evidence format, labels, or scoring. "
        "Do not write as if you are analyzing a document: forbidden phrases and patterns include "
        '"based on the provided", "the text", "structured claims", "key points:", '
        '"prose integration", "I will summarize", "note that without verified", '
        "outline-of-the-answer meta, or section headers inside this section other than normal paragraphs.\n"
        "## Summary\n"
        "A standalone, detailed executive summary: **minimum 300 words, target 300–350 words** "
        "(count before finishing). "
        "Multi-paragraph recap of the answer, main trade-offs, risks, and recommendations implied by the evidence. "
        "No bullets unless essential; no meta-commentary; do not say you are summarizing—just deliver the summary.\n"
        "## Findings\n"
        "Expand with bullets or short paragraphs grounded in the claims; cite with [source_id].\n"
        "## Caveats\n"
        "Limitations, weak evidence, or verification gaps.\n"
        "Throughout: weave claim content into sentences; do not paste the raw claim list or trust flags as the body. "
        "If evidence is thin, still answer in domain terms and state clearly what is unknown.\n"
        "Citation rules: inline [source_id] copied verbatim from the Citation catalog only. "
        "Never use ordinal placeholders (e.g. src_1) or ids absent from that catalog.\n"
        "If the catalog is empty or a claim lacks sources, cite nothing for that clause."
    )
    user = (
        f"User question — answer it substantively (not with a generic product overview unless that is the question):\n"
        f"{state['user_query']}\n\n"
        f"Citation catalog (valid [source_id] keys only):\n"
        f"{catalog_txt or '(none — do not cite source ids)'}\n\n"
        f"Evidence lines (HIGH/MODERATE/LOW trust — use as factual material, do not quote or label this block in the output):\n{block}"
    )
    text, cost = await chat_text(
        model=settings.model_strong,
        system=system,
        user=user,
        temperature=0.26,
        top_p=0.88,
        max_tokens=5120,
        session_id=str(state["session_id"]),
        generation_name="synthesizer",
    )
    text = _expand_catalog_citations(text.strip(), catalog)
    text = _strip_placeholder_numeric_citations(text, catalog)
    return {
        "draft_report": text,
        "agent_invocations": 1,
        "cost_usd": cost,
        "messages": ["Synthesizer completed draft report."],
    }


async def citation_formatter_node(state: ResearchGraphState) -> dict[str, Any]:
    """
    Verify claim–source alignment via similarity; annotate LOW_CONFIDENCE flags.

    This is a separate LLM-light pass plus embedding/TF-IDF similarity as in spec.
    """
    settings = get_settings()
    catalog, _ = _source_catalog(state)
    threshold = settings.citation_similarity_threshold

    report_lines: list[str] = [state.get("draft_report") or ""]

    for claim in state.get("verified_claims") or []:
        text = claim.get("claim") or ""
        worst = 1.0
        for sid in claim.get("source_ids") or []:
            src = catalog.get(sid)
            if not src:
                continue
            basis = str(src.get("snippet") or "")[:4000]
            if src.get("full_content"):
                basis = str(src.get("full_content"))[:4000]
            sim = await similarity_score(
                text,
                basis,
                session_id=str(state["session_id"]),
                generation_name="citation_similarity",
            )
            worst = min(worst, sim)
        flags = list(claim.get("flags") or [])
        if worst < threshold:
            flags.append("LOW_CITATION_ALIGNMENT")
        if flags:
            report_lines.append(
                f"\n> **Citation review** for claim “{text[:160]}…” — "
                f"similarity min={worst:.2f} (threshold {threshold}). Flags: {', '.join(flags)}"
            )

    final = "\n".join(report_lines).strip()
    final = _expand_catalog_citations(final, catalog)
    final = _strip_placeholder_numeric_citations(final, catalog)
    return {
        "final_report": final,
        "agent_invocations": 1,  # nominal accounting for formatting pass
        "cost_usd": 0.0,
        "messages": ["Citation formatter completed."],
    }

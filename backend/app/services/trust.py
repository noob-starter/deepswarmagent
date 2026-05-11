"""
Heuristic trust scoring — auditable, no black box.

Five dimensions from the product spec (simplified, open-source friendly):
- source_count
- source_authority (domain tier)
- source_agreement (placeholder: neutral mid-score without an NLI model)
- recency (from normalized date string if present)
- fact_checker (injected after verification pass)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from app.schemas.state import ClaimDict, SourceDict


def _hostname_is_archive_org(hostname: str) -> bool:
    """True for archive.org / *.archive.org (e.g. web.archive.org); not unrelated *archive.org typos."""
    parts = hostname.lower().split(".")
    return len(parts) >= 2 and parts[-2] == "archive" and parts[-1] == "org"


def _domain_authority(url: str) -> float:
    """Bucketed authority score 0–100 based on registrable domain heuristics."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return 30.0
    if not host:
        return 30.0
    if host.endswith(".gov") or host.endswith(".mil"):
        return 95.0
    if host.endswith(".edu"):
        return 88.0
    if host.endswith("wikipedia.org"):
        return 85.0
    # Internet Archive snapshots are a stable “source of truth” pointer to prior pages.
    if _hostname_is_archive_org(host):
        return 78.0
    if host.endswith(".org"):
        return 65.0
    if host in {"github.com", "arxiv.org"}:
        return 80.0
    return 55.0


def domain_authority_for_url(url: str) -> float:
    """Public hook for ranking catalog entries (same signal as trust breakdown)."""
    return _domain_authority(url)


def is_internet_archive_snapshot_url(url: str) -> bool:
    """True when host is archive.org or *.archive.org (includes web.archive.org)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return bool(host) and _hostname_is_archive_org(host)


def _recency_score(published_date: str | None) -> float:
    """
    Map ISO-like date strings to a 0–100 score, favoring recent sources.

    Unknown dates return a neutral 60 so they do not dominate the average.
    """
    if not published_date:
        return 60.0
    try:
        raw = published_date.strip()
        normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
        # 0 days ~100, 5 years ~20
        return float(max(20.0, min(100.0, 100.0 - age_days / 1825.0 * 80.0)))
    except ValueError:
        return 60.0


def compute_trust_breakdown(
    claim: str,
    sources: list[SourceDict],
    fact_checker_score: float | None,
) -> dict[str, Any]:
    """Return per-dimension scores and the integer 0–100 headline trust score."""
    if not sources:
        fc = float(fact_checker_score or 0.0)
        return {
            "source_count": 0.0,
            "source_authority": 0.0,
            "source_agreement": 40.0,
            "recency": 40.0,
            "fact_checker": fc,
            "_trust": fc * 0.3,
            "_trust_int": int(max(0, min(45, round(fc * 0.45)))),
        }

    n = len(sources)
    count_score = min(100.0, 35.0 + 15.0 * max(0, n - 1))
    auth = sum(_domain_authority(s.get("url") or "") for s in sources) / n
    recency = sum(_recency_score(s.get("published_date")) for s in sources) / n
    agreement = 70.0  # placeholder: upgrade with entailment model later
    fc = fact_checker_score if fact_checker_score is not None else 55.0

    weights = {
        "source_count": 0.15,
        "source_authority": 0.25,
        "source_agreement": 0.15,
        "recency": 0.15,
        "fact_checker": 0.30,
    }
    parts = {
        "source_count": count_score,
        "source_authority": auth,
        "source_agreement": agreement,
        "recency": recency,
        "fact_checker": fc,
    }
    trust = sum(parts[k] * weights[k] for k in weights)
    trust_int = int(max(0, min(100, round(trust))))
    return {**parts, "_trust": trust, "_trust_int": trust_int}


def attach_trust_to_claim(
    claim: ClaimDict,
    id_to_source: dict[str, SourceDict],
    fact_checker_score: float | None,
) -> ClaimDict:
    """Mutate-and-return pattern for clarity in nodes."""
    srcs = [id_to_source[i] for i in claim.get("source_ids", []) if i in id_to_source]
    breakdown = compute_trust_breakdown(claim.get("claim", ""), srcs, fact_checker_score)
    trust = int(breakdown.pop("_trust_int", 0))
    breakdown.pop("_trust", None)
    claim = {**claim, "trust_score": trust, "trust_breakdown": breakdown}
    return claim

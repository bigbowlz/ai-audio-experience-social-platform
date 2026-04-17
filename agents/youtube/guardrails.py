"""Hallucination guardrails: deterministic claim_kind and provenance_shape.

Pure functions — no I/O, no LLM calls. These constrain what the LLM
is allowed to claim in pitch hooks.

Spec: agents/docs/prompt_design.md §1–§2
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.youtube.extractor import Contributor


class ClaimKind(str, Enum):
    DURABLE = "durable"
    RISING = "rising"
    DISCOVERY = "discovery"
    NEUTRAL = "neutral"


class ProvenanceShape(str, Enum):
    BALANCED = "balanced"
    SUB_ONLY = "sub_only"
    LIKE_ONLY = "like_only"


def compute_claim_kind(
    topic: str,
    long_term: dict[str, float],
    recent: dict[str, float],
    provenance: list[Contributor],
    total_recent_weight: float,
) -> ClaimKind:
    """Deterministic temporal-framing constraint per candidate topic.

    Evaluation order: rising → discovery → durable → neutral.
    First match wins.
    """
    sub_count = sum(1 for c in provenance if c["kind"] == "sub")
    like_count = sum(1 for c in provenance if c["kind"] == "like")
    lt = long_term.get(topic, 0.0)
    rt = recent.get(topic, 0.0)

    if lt > 0 and rt > lt and like_count >= 3 and total_recent_weight >= 2.0:
        return ClaimKind.RISING
    if lt == 0.0 and like_count >= 2:
        return ClaimKind.DISCOVERY
    if lt > 0 and sub_count >= 2:
        return ClaimKind.DURABLE
    return ClaimKind.NEUTRAL


def compute_provenance_shape(provenance: list[Contributor]) -> ProvenanceShape:
    """Deterministic evidence-framing constraint from contributor list."""
    has_sub = any(c["kind"] == "sub" for c in provenance)
    has_like = any(c["kind"] == "like" for c in provenance)
    if has_sub and has_like:
        return ProvenanceShape.BALANCED
    if has_sub:
        return ProvenanceShape.SUB_ONLY
    if has_like:
        return ProvenanceShape.LIKE_ONLY
    return ProvenanceShape.BALANCED

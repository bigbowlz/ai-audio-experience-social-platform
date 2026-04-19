"""LLM hook generation for YouTubeAgent pitch() — Layer 3.

Replaces the Layer 2 template hooks with a constrained Claude call.
The LLM selects 3–5 candidates from the algo-assembled bundle and
writes hooks governed by claim_kind + provenance_shape.

Spec: agents/docs/prompt_design.md §1–§2
      agents/youtube/docs/DESIGN.md §pitch() flow
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[2] / ".env")

from agents.protocol import Brief, Pitch
from agents.youtube.extractor import Contributor
from agents.youtube.guardrails import ClaimKind, ProvenanceShape

# Prompt text lives in agents/youtube/prompts.py. Re-exported here so
# existing callers that import from this module continue to work.
from agents.youtube.prompts import (
    PATRICKS_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    YOUTUBE_SYSTEM_PROMPT,
    system_prompt_for as _system_prompt_for,
)

# ── Constants ────────────────────────────────────────────────────────

MODEL = os.environ.get("YOUTUBE_LLM_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 2048


# ── Candidate formatting ─────────────────────────────────────────────


def _format_contributor(c: Contributor) -> dict[str, Any]:
    """Slim contributor for prompt context."""
    out: dict[str, Any] = {"kind": c["kind"], "channel": c["channel_name"]}
    if c["kind"] == "sub" and c["subscribed_at"]:
        out["subscribed_at"] = c["subscribed_at"][:10]  # date only
    if c["kind"] == "like":
        if c["video_title"]:
            out["video_title"] = c["video_title"]
        if c["liked_at"]:
            out["liked_at"] = c["liked_at"][:10]
    return out


def _format_bundle(
    bundle: list[dict[str, Any]],
    brief: Brief,
) -> str:
    """Format the candidate bundle + brief as the user message."""
    candidates = []
    for item in bundle:
        candidates.append(
            {
                "topic": item["topic"],
                "score": round(item["score"], 4),
                "long_term": round(item["long_term"], 4),
                "recent": round(item["recent"], 4),
                "claim_kind": (
                    item["claim_kind"].value
                    if isinstance(item["claim_kind"], ClaimKind)
                    else item["claim_kind"]
                ),
                "provenance_shape": (
                    item["provenance_shape"].value
                    if isinstance(item["provenance_shape"], ProvenanceShape)
                    else item["provenance_shape"]
                ),
                "provenance": [_format_contributor(c) for c in item["provenance"]],
            }
        )

    payload = {
        "candidates": candidates,
        "today_context": brief["today_context"],
    }
    return json.dumps(payload, indent=2)


# ── LLM call ─────────────────────────────────────────────────────────


def generate_pitches(
    bundle: list[dict[str, Any]],
    brief: Brief,
    agent_name: str = "youtube",
) -> list[Pitch]:
    """Call Claude to select 3–5 candidates and write constrained hooks.

    Falls back to template hooks if the LLM call fails or returns
    unparseable output.
    """
    if os.environ.get("DISABLE_LLM"):
        raise RuntimeError("LLM disabled via DISABLE_LLM env var")

    client = anthropic.Anthropic()
    user_msg = _format_bundle(bundle, brief)

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=_system_prompt_for(agent_name),
        messages=[{"role": "user", "content": user_msg}],
        timeout=30.0,
    )

    if not response.content or response.content[0].type != "text":
        raise ValueError("LLM returned no text content")
    raw = response.content[0].text.strip()
    # Strip markdown fences if the model wraps anyway
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3].strip()

    selections = json.loads(raw)

    # Build a lookup for bundle items by topic
    bundle_by_topic = {item["topic"]: item for item in bundle}

    pitches: list[Pitch] = []
    for sel in selections:
        topic = sel["topic"]
        if topic not in bundle_by_topic:
            continue  # LLM selected a topic not in the bundle — skip

        item = bundle_by_topic[topic]
        contributors: list[Contributor] = item["provenance"]
        claim_kind = item["claim_kind"]
        prov_shape = item["provenance_shape"]

        # source_refs from provenance (human-readable names for downstream LLM consumption)
        source_refs = []
        for c in contributors:
            if c["kind"] == "sub":
                source_refs.append(c["channel_name"])
            elif c["video_title"]:
                source_refs.append(c["video_title"])
            else:
                source_refs.append(c["channel_name"])

        pitches.append(
            Pitch(
                agent=agent_name,
                title=sel.get("title", topic.replace("-", " ").title()),
                hook=sel["hook"],
                source_refs=list(dict.fromkeys(source_refs)),
                priority=min(1.0, max(0.0, float(sel.get("priority", item["score"])))),
                thin_signal=False,
                claim_kind=(
                    claim_kind.value
                    if isinstance(claim_kind, ClaimKind)
                    else claim_kind
                ),
                provenance_shape=(
                    prov_shape.value
                    if isinstance(prov_shape, ProvenanceShape)
                    else prov_shape
                ),
            )
        )

    return pitches

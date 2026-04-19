"""AlicesAgent: external creator agent backed by Alice's YouTube data.

Shares the extraction pipeline with YouTubeAgent but loads pre-captured
Day-0 JSON instead of live OAuth. Implements the DataAgent protocol with
external=True, price_usdc=0.10, and a Base Sepolia wallet_address.

Spec: agents/youtube/docs/DESIGN.md §Shared extractor
      agents/docs/DESIGN.md §Interface contract
"""

from __future__ import annotations

import json
import logging
import os
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from agents.protocol import (
    AgentMemory,
    Brief,
    DataAgent,
    Pitch,
    ScopeContext,
    bootstrap_memory,
)
from agents.youtube.extractor import (
    Contributor,
    InterestProfile,
    extract_profile,
)
from agents.youtube.guardrails import (
    ClaimKind,
    ProvenanceShape,
    compute_claim_kind,
    compute_provenance_shape,
)
from agents.youtube.llm import generate_pitches as llm_generate_pitches

log = logging.getLogger(__name__)

# ── Data path ────────────────────────────────────────────────────────
# Override via PATRICKS_DATA_DIR env var; defaults to committed Day-0 data.

_HERE = Path(__file__).resolve().parent
_DEFAULT_DATA_DIR = _HERE / "data"
DATA_DIR = Path(os.environ.get("PATRICKS_DATA_DIR", _DEFAULT_DATA_DIR))

# ── Algo constants (same as YouTubeAgent) ────────────────────────────

CANDIDATE_N = 8
MAX_PITCHES = 5
MIN_PITCHES = 3


# ── Data loader ──────────────────────────────────────────────────────


def _load_data(
    data_dir: Path,
) -> tuple[list[dict], list[dict], dict[str, list[str]], dict[str, list[str]]]:
    """Load Alice's captured JSON and return (subs, likes, channel_topics, video_topics).

    Same format as YouTubeAgent's _load_probe_data — both consume the
    shared extractor's input contract.
    """
    with open(data_dir / "02_subscriptions.json") as f:
        subs = json.load(f)["items"]

    with open(data_dir / "03_likes.json") as f:
        likes = json.load(f)["items"]

    with open(data_dir / "07_topic_details.json") as f:
        chan_raw = json.load(f)
    channel_topics: dict[str, list[str]] = {}
    for item in chan_raw.get("items", []):
        cats = item.get("topicDetails", {}).get("topicCategories", [])
        if cats:
            channel_topics[item["id"]] = cats

    with open(data_dir / "08_video_topic_details.json") as f:
        vid_raw = json.load(f)
    video_topics: dict[str, list[str]] = {}
    for entry in vid_raw.get("per_video", []):
        tags = entry.get("tags", [])
        if tags:
            video_topics[entry["id"]] = tags

    return subs, likes, channel_topics, video_topics


# ── Deterministic candidate selection ────────────────────────────────


def _top_n_seeded(score: dict[str, float], n: int, seed: tuple) -> list[str]:
    rng = random.Random(str(seed))
    keyed = [(v, rng.random(), k) for k, v in score.items()]
    keyed.sort(reverse=True)
    return [k for _, _, k in keyed[: min(n, len(keyed))]]


# ── Template hook generation (fallback) ──────────────────────────────


def _topic_label(topic: str) -> str:
    return topic.replace("-", " ")


def _template_hook(
    topic: str,
    claim_kind: ClaimKind,
    contributors: list[Contributor],
) -> str:
    """Structured what/source/goal brief to the Producer.

    External-curator semantics are explicit: this pitch reflects Alice's
    taste, NOT the listener's — the Producer must narrate it as curator
    recommendation, never as "here's something you searched for".
    """
    label = _topic_label(topic)
    subs = [c for c in contributors if c["kind"] == "sub"]
    likes = [c for c in contributors if c["kind"] == "like"]

    if claim_kind == ClaimKind.RISING:
        ref = likes[0]["channel_name"] if likes else label
        evidence = f"{ref} trending in Alice's recent likes"
    elif claim_kind == ClaimKind.DISCOVERY:
        if likes and likes[0]["video_title"]:
            ref = f'"{likes[0]["video_title"]}" on {likes[0]["channel_name"]}'
        elif likes:
            ref = likes[0]["channel_name"]
        else:
            ref = label
        evidence = f"Alice recently surfaced {ref}"
    elif claim_kind == ClaimKind.DURABLE:
        ref = subs[0]["channel_name"] if subs else label
        since = ""
        if subs and subs[0].get("subscribed_at"):
            since = f" (since {subs[0]['subscribed_at'][:4]})"
        evidence = f"Alice has been subscribed to {ref}{since}"
    else:  # NEUTRAL
        if contributors:
            ref = contributors[0]["channel_name"]
            evidence = f"{label} appeared in Alice's YouTube activity via {ref}"
        else:
            evidence = f"{label} appeared in Alice's YouTube data"

    return (
        f"WHAT: Curator recommendation on {label} (claim_kind={claim_kind.value}) — {evidence}.\n"
        f"SOURCE: @GoddamnAxl (external curator, pre-captured Day-0 data) — NOT the listener's own interest.\n"
        f"GOAL: Expose the listener to Alice's taste. "
        f"Narrate as curator pick ('Alice's been into X', 'Alice flagged Y'), "
        f"never as listener taste ('you've been into X'). "
        f"Respect claim_kind directives for temporal framing."
    )


# ── Thin-signal pitch ────────────────────────────────────────────────


def _thin_signal_pitch() -> Pitch:
    return Pitch(
        agent="alices",
        title="Alice's YouTube world",
        hook=(
            "WHAT: General-interest curator segment in Alice's domain (photography, travel, tech). "
            "No specific topic ranking available.\n"
            "SOURCE: @GoddamnAxl (external curator, pre-captured Day-0 data) — NOT the listener's own interest.\n"
            "GOAL: Write a general-interest segment framed as Alice's curator voice. "
            "Do not personalize to the listener; no channels/subs/videos by name."
        ),
        source_refs=[],
        priority=0.3,
        thin_signal=True,
        claim_kind="neutral",
        provenance_shape="balanced",
    )


# ── AlicesAgent ────────────────────────────────────────────────────


class AlicesAgent:
    """External creator agent backed by Alice's pre-captured YouTube data."""

    name: str = "alices"
    display_name: str = "@GoddamnAxl"
    scope: str = "Alice's YouTube world — photography, travel, tech"
    external: bool = True
    price_usdc: float | None = 0.10
    wallet_address: str | None = "0x8043AeeD92c681492B13f46e91EFb8B42D18E3b2"

    def load_memory(self, user_id: str) -> AgentMemory:
        return bootstrap_memory()

    def fetch_context(self, user_id: str) -> ScopeContext:
        now = datetime.now(timezone.utc)
        subs, likes, channel_topics, video_topics = _load_data(DATA_DIR)
        profile = extract_profile(subs, likes, channel_topics, video_topics, now)
        ctx: ScopeContext = cast(ScopeContext, {"profile": profile})
        return ctx

    def pitch(
        self,
        brief: Brief,
        memory: AgentMemory,
        context: ScopeContext,
        user_id: str,
    ) -> list[Pitch]:
        profile: InterestProfile = cast(ScopeContext, context)["profile"]  # type: ignore[index]

        if not profile["combined_topic_scores"]:
            return [_thin_signal_pitch()]

        # ── Step 1: algo — candidate assembly (deterministic) ────────
        multiplier = memory["topic_multiplier"]
        score = {
            t: profile["combined_topic_scores"][t] * multiplier.get(t, 1.0)
            for t in profile["combined_topic_scores"]
        }

        seed = (user_id, profile["computed_at"])
        candidates = _top_n_seeded(score, n=CANDIDATE_N, seed=seed)

        bundle = []
        for t in candidates:
            provenance = profile["topic_provenance"].get(t, [])
            bundle.append(
                {
                    "topic": t,
                    "score": score[t],
                    "long_term": profile["long_term_topic_scores"].get(t, 0.0),
                    "recent": profile["recent_topic_scores"].get(t, 0.0),
                    "provenance": provenance,
                    "claim_kind": compute_claim_kind(
                        t,
                        profile["long_term_topic_scores"],
                        profile["recent_topic_scores"],
                        provenance,
                        profile["stats"]["total_recent_weight"],
                    ),
                    "provenance_shape": compute_provenance_shape(provenance),
                }
            )

        if len(bundle) < MIN_PITCHES:
            return [_thin_signal_pitch()]

        # ── Step 2: LLM selection + hook generation ──────────────────
        try:
            pitches = llm_generate_pitches(bundle, brief, agent_name="alices")
            if MIN_PITCHES <= len(pitches) <= MAX_PITCHES:
                return pitches
            log.warning(
                "LLM returned %d pitches (expected %d–%d), falling back to templates",
                len(pitches),
                MIN_PITCHES,
                MAX_PITCHES,
            )
        except Exception:
            log.warning(
                "LLM hook generation failed, falling back to templates", exc_info=True
            )

        # ── Fallback: template hooks ─────────────────────────────────
        n_pitches = min(MAX_PITCHES, len(bundle))
        selected = bundle[:n_pitches]

        pitches_fallback: list[Pitch] = []
        for item in selected:
            topic = item["topic"]
            claim_kind: ClaimKind = item["claim_kind"]
            prov_shape: ProvenanceShape = item["provenance_shape"]
            contributors: list[Contributor] = item["provenance"]

            hook = _template_hook(topic, claim_kind, contributors)

            source_refs = []
            for c in contributors:
                if c["kind"] == "sub":
                    source_refs.append(c["channel_id"])
                elif c["video_id"]:
                    source_refs.append(c["video_id"])

            pitches_fallback.append(
                Pitch(
                    agent="alices",
                    title=_topic_label(topic).title(),
                    hook=hook,
                    source_refs=list(dict.fromkeys(source_refs)),
                    priority=min(1.0, round(item["score"], 4)),
                    thin_signal=False,
                    claim_kind=claim_kind.value,
                    provenance_shape=prov_shape.value,
                )
            )

        return pitches_fallback

"""YouTubeAgent: DataAgent implementation for user YouTube signals.

fetch_context() loads probe JSON (dev) and calls the shared extractor.
pitch() runs the deterministic algo step, then calls the LLM for hook
generation (Layer 3). Falls back to template hooks on LLM failure.

Spec: agents/youtube/docs/DESIGN.md
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
)
from learning_loop import load_agent_memory
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

# ── Probe data path (dev) ────────────────────────────────────────────
# Override via YOUTUBE_PROBE_DIR env var; defaults to the committed dev probe.

_REPO_ROOT = Path(__file__).parents[2]
_DEFAULT_PROBE_DIR = _REPO_ROOT / "tmp" / "ydata" / "probe_1776208130"
PROBE_DIR = Path(os.environ.get("YOUTUBE_PROBE_DIR", _DEFAULT_PROBE_DIR))

# ── Algo constants ────────────────────────────────────────────────────

CANDIDATE_N = 8     # over-select; LLM (Layer 3) picks 3–5 from these
MAX_PITCHES = 5
MIN_PITCHES = 3


# ── Probe data loader ────────────────────────────────────────────────

def _load_probe_data(
    probe_dir: Path,
) -> tuple[list[dict], list[dict], dict[str, list[str]], dict[str, list[str]]]:
    """Load dev probe JSONs and return (subs, likes, channel_topics, video_topics).

    channel_topics: channel_id → list of Wikipedia topic URLs
    video_topics:   video_id  → list of topic page names (e.g. "Rock_music")
    Both formats are handled by extractor.normalize_topic().
    """
    with open(probe_dir / "02_subscriptions.json") as f:
        subs = json.load(f)["items"]

    with open(probe_dir / "03_likes.json") as f:
        likes = json.load(f)["items"]

    with open(probe_dir / "07_topic_details.json") as f:
        chan_raw = json.load(f)
    channel_topics: dict[str, list[str]] = {}
    for item in chan_raw.get("items", []):
        cats = item.get("topicDetails", {}).get("topicCategories", [])
        if cats:
            channel_topics[item["id"]] = cats

    with open(probe_dir / "08_video_topic_details.json") as f:
        vid_raw = json.load(f)
    video_topics: dict[str, list[str]] = {}
    for entry in vid_raw.get("per_video", []):
        tags = entry.get("tags", [])
        if tags:
            video_topics[entry["id"]] = tags

    return subs, likes, channel_topics, video_topics


# ── Deterministic candidate selection ────────────────────────────────

def _top_n_seeded(score: dict[str, float], n: int, seed: tuple) -> list[str]:
    """Return top-n topics by score, ties broken deterministically via seed.

    Uses a seeded random tiebreaker assigned upfront so the sort is stable
    across calls with the same (user_id, computed_at) seed.
    """
    rng = random.Random(str(seed))
    keyed = [(v, rng.random(), k) for k, v in score.items()]
    keyed.sort(reverse=True)
    return [k for _, _, k in keyed[:min(n, len(keyed))]]


# ── Template hook generation (Layer 2 stub; replaced by LLM in Layer 3) ──

def _topic_label(topic: str) -> str:
    return topic.replace("-", " ")


def _template_hook(
    topic: str,
    claim_kind: ClaimKind,
    provenance_shape: ProvenanceShape,
    contributors: list[Contributor],
) -> str:
    """Generate a deterministic hook string from claim_kind + provenance.

    Layer 3 replaces this with a constrained LLM call. The template
    is intentionally flat — its job is end-to-end pipeline correctness,
    not narrative quality.
    """
    label = _topic_label(topic)
    subs = [c for c in contributors if c["kind"] == "sub"]
    likes = [c for c in contributors if c["kind"] == "like"]

    if claim_kind == ClaimKind.RISING:
        ref = likes[0]["channel_name"] if likes else label
        return (
            f"You've been getting into {label} lately — "
            f"{ref} has been showing up in your feed more and more."
        )

    if claim_kind == ClaimKind.DISCOVERY:
        if likes and likes[0]["video_title"]:
            ref = f'"{likes[0]["video_title"]}" on {likes[0]["channel_name"]}'
        elif likes:
            ref = likes[0]["channel_name"]
        else:
            ref = label
        return f"Some {label} has caught your eye recently — {ref}."

    if claim_kind == ClaimKind.DURABLE:
        ref = subs[0]["channel_name"] if subs else label
        since = ""
        if subs and subs[0].get("subscribed_at"):
            since = f" since {subs[0]['subscribed_at'][:4]}"
        return (
            f"You've been into {label} for a while — "
            f"subscribed to {ref}{since}."
        )

    # NEUTRAL: state facts, no temporal claims
    if contributors:
        ref = contributors[0]["channel_name"]
        return f"{label.title()} showed up in your YouTube activity — {ref}."
    return f"{label.title()} appeared in your YouTube data."


# ── Thin-signal pitch ─────────────────────────────────────────────────

def _thin_signal_pitch() -> Pitch:
    return Pitch(
        agent="youtube",
        title="Your YouTube world",
        hook=(
            "Not enough signal yet to personalize. "
            "Pitch a general-interest segment in the YouTube domain."
        ),
        source_refs=[],
        priority=0.3,
        thin_signal=True,
        claim_kind="neutral",
        provenance_shape="balanced",
    )


# ── YouTubeAgent ─────────────────────────────────────────────────────

class YouTubeAgent:
    """Internal DataAgent backed by the user's YouTube subscriptions + likes."""

    name: str = "youtube"
    display_name: str = "@YouTube"
    scope: str = "YouTube subscriptions and liked videos"
    external: bool = False
    price_usdc: float | None = None
    wallet_address: str | None = None

    def load_memory(self, user_id: str) -> AgentMemory:
        """Return current AgentMemory for this user.

        v0: routes through learning_loop.load_agent_memory, which returns
        any seeded topic_multiplier (via seed_topic_multiplier) and falls
        back to bootstrap_memory() otherwise. No persistent store yet.
        v1+: read from api-storage agent_memory table.
        """
        return load_agent_memory(user_id, self.name)

    def fetch_context(self, user_id: str) -> ScopeContext:
        """Build an InterestProfile from YouTube data and return it as ScopeContext.

        v0: loads from committed dev probe JSON (PROBE_DIR).
        v1+: fetches live from YouTube Data API v3 with user OAuth.

        Write-through: on success, the profile would overwrite memory.profile_state.
        On failure, the prior profile is reused (no-op write). v0 always succeeds
        because the probe JSON is committed.
        """
        now = datetime.now(timezone.utc)
        subs, likes, channel_topics, video_topics = _load_probe_data(PROBE_DIR)
        profile = extract_profile(subs, likes, channel_topics, video_topics, now)
        # Cast to ScopeContext — the orchestrator reads context["profile"]
        ctx: ScopeContext = cast(ScopeContext, {"profile": profile})
        return ctx

    def pitch(
        self,
        brief: Brief,
        memory: AgentMemory,
        context: ScopeContext,
        user_id: str,
    ) -> list[Pitch]:
        """Deterministic algo step + template hooks (Layer 2 stub).

        Output contract: 3–5 Pitch objects, or exactly 1 thin-signal Pitch.
        """
        profile: InterestProfile = cast(ScopeContext, context)["profile"]  # type: ignore[index]

        # ── Empty-profile short-circuit ──────────────────────────────
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

        # Build candidate bundle (mirrors what would go to LLM in Layer 3)
        bundle = []
        for t in candidates:
            provenance = profile["topic_provenance"].get(t, [])
            bundle.append({
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
            })

        # ── Sparse-topic guard: fall back to thin-signal ─────────────
        # Protocol contract: 3–5 pitches or exactly 1 thin-signal.
        # If fewer than MIN_PITCHES candidates exist, the profile is too
        # sparse for ranked topic pitches — emit thin-signal instead.
        if len(bundle) < MIN_PITCHES:
            return [_thin_signal_pitch()]

        # ── Step 2: LLM selection + hook generation (Layer 3) ────────
        # Call the LLM to pick 3–5 from the bundle and write hooks
        # constrained by claim_kind + provenance_shape.
        # Falls back to template hooks on any LLM failure.
        try:
            pitches = llm_generate_pitches(bundle, brief)
            if MIN_PITCHES <= len(pitches) <= MAX_PITCHES:
                return pitches
            log.warning(
                "LLM returned %d pitches (expected %d–%d), falling back to templates",
                len(pitches), MIN_PITCHES, MAX_PITCHES,
            )
        except Exception:
            log.warning("LLM hook generation failed, falling back to templates", exc_info=True)

        # ── Fallback: template hooks (Layer 2) ──────────────────────
        n_pitches = min(MAX_PITCHES, len(bundle))
        selected = bundle[:n_pitches]

        pitches_fallback: list[Pitch] = []
        for item in selected:
            topic = item["topic"]
            claim_kind: ClaimKind = item["claim_kind"]
            prov_shape: ProvenanceShape = item["provenance_shape"]
            contributors: list[Contributor] = item["provenance"]

            hook = _template_hook(topic, claim_kind, prov_shape, contributors)

            source_refs = []
            for c in contributors:
                if c["kind"] == "sub":
                    source_refs.append(c["channel_name"])
                elif c["video_title"]:
                    source_refs.append(c["video_title"])
                else:
                    source_refs.append(c["channel_name"])

            pitches_fallback.append(Pitch(
                agent="youtube",
                title=_topic_label(topic).title(),
                hook=hook,
                source_refs=list(dict.fromkeys(source_refs)),
                priority=min(1.0, round(item["score"], 4)),
                thin_signal=False,
                claim_kind=claim_kind.value,
                provenance_shape=prov_shape.value,
            ))

        return pitches_fallback

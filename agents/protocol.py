"""Shared DataAgent protocol and core data shapes.

All agents implement DataAgent. The orchestrator talks exclusively
through this interface — no agent-specific escape hatches.

Spec: agents/docs/DESIGN.md §Interface contract
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import NotRequired, TypedDict, Protocol, runtime_checkable


# ── Today's context (assembled by orchestrator from weather + calendar) ──


class TodayContext(TypedDict, total=False):
    date: str  # ISO 8601 date, e.g. "2026-04-16"
    day_of_week: str  # "Monday" … "Sunday"
    time_of_day: str  # "morning" | "afternoon" | "evening" | "night"
    now: str  # 24-hour local time only, HH:MM:SS — date already carried by `date` field
    weather_summary: str | None  # "rainy, 14°C" — None if weather fetch failed
    calendar_events: (
        list[str] | None
    )  # ["Team standup 10am"] — None if no calendar agent


# ── UserProfile: identity fields (separate from TodayContext, which is "what's true today") ──


class UserProfile(TypedDict, total=False):
    """Per-user identity fields the Producer may use in spoken script.

    `total=False` because any field may be absent — Producer must tolerate None.
    Sourced from Google OAuth (openid profile scope) via auth/calendar_auth.py
    and cached at ~/.config/radio-podcast/user_profile.json. Absent for
    any user who has not completed auth.
    """

    first_name: str  # "Alice" — preferred salutation in cold open
    display_name: str  # "Alice Guesto" — full name; v0 informational only


# ── Brief: per-episode context object, same for all agents ──


class Brief(TypedDict):
    today_context: TodayContext
    user_profile: NotRequired[
        UserProfile | None
    ]  # absent or None = address user as "you"


# ── ScopeContext: base shape; each agent extends it with its own fields ──
#
#   youtube_agent  → {"profile": InterestProfile}
#   weather_agent  → {"weather_summary": str}
#   calendar_agent → {"calendar_events": list[str]}


class ScopeContext(TypedDict, total=False):
    """Base shape. Agents add their own fields."""

    pass


# ── Pitch: what an agent emits ──


class Pitch(TypedDict, total=False):
    agent: str  # "youtube" | "calendar" | "weather" | "external"
    title: str
    hook: str  # creative brief for Producer — not spoken verbatim
    data: dict  # structured payload; agent-specific (e.g. calendar events)
    source_refs: list[str]  # channel_ids / video_ids / etc.
    priority: float  # [0, 1]; higher = more important
    thin_signal: bool  # True iff exactly 1 pitch due to insufficient data
    claim_kind: str  # "durable" | "rising" | "discovery" | "neutral"
    provenance_shape: str  # "balanced" | "sub_only" | "like_only"


# ── RunningOrder: Producer's selected segments + episode-level metadata ──


class RunningOrder(TypedDict):
    """Producer's output of select_guaranteed_slots + select_bonus_segments_llm.

    Replaces the implicit `list[Pitch]` running-order shape used through 2026-04-17.
    The same Pitch objects appear under `segments`; the wrapper carries
    episode-level metadata that today's tuple returns smuggle separately.
    """

    segments: list[Pitch]  # ordered: guaranteed first, then bonus
    total_sec: int  # sum of suggested_length_sec for all segments
    guaranteed_count: int  # how many of `segments` are guaranteed
    bonus_count: int  # len(segments) - guaranteed_count


# ── ExternalDecision: Producer's call on whether to invoke an external agent ──


class ExternalDecision(TypedDict):
    """Result of producer.external.decide_external_invocation()."""

    decision: str  # "invoke" | "skip"; v0 always "invoke"
    rationale: str  # human-readable; surfaced in SSE event payload


# ── CreatorAgentListing: marketplace entry for an external agent ──


class CreatorAgentListing(TypedDict):
    """Result of producer.external.query_marketplace().

    v0 reads a hardcoded list. v1 queries a real marketplace.
    """

    handle: str  # "@GoddamnAxl"
    display_name: str  # "External Lens"
    scope: str  # human-readable scope description
    price_usdc: float  # demo: 0.10
    wallet_address: str  # Base Sepolia address


# ── AgentMemory: persisted per-(user, agent) state ──


class AgentMemory(TypedDict):
    schema_version: int  # = 1 for v0
    profile_state: dict  # InterestProfile; owned by agents/youtube
    topic_multiplier: dict[str, float]  # owned by learning-loop
    updated_at: str  # ISO 8601; bumped on any field write


def bootstrap_memory() -> AgentMemory:
    """Default AgentMemory for a (user, agent) pair that has never had a row.

    Lazy creation: the row is persisted on the first real write. This default
    is returned in-memory so pitch() can proceed without a DB round-trip.
    """
    now = datetime.now(timezone.utc).isoformat()
    return AgentMemory(
        schema_version=1,
        profile_state={
            "long_term_topic_scores": {},
            "recent_topic_scores": {},
            "combined_topic_scores": {},
            "topic_provenance": {},
            "computed_at": now,
            "stats": {
                "total_subs": 0,
                "total_likes": 0,
                "total_recent_weight": 0.0,
                "unique_topics": 0,
                "tag_coverage_pct": 0.0,
                "avg_topics_per_entity": 0.0,
            },
        },
        topic_multiplier={},
        updated_at=now,
    )


# ── DataAgent protocol ──


@runtime_checkable
class DataAgent(Protocol):
    name: str  # "youtube" | "calendar" | "weather" | "external"
    display_name: str  # "@YouTube" | curator handle (e.g. "@GoddamnAxl")
    scope: str  # human-readable scope description
    external: bool  # True for creator agents only
    price_usdc: float | None  # None for internal agents
    wallet_address: str | None  # None for internal agents

    def load_memory(self, user_id: str) -> AgentMemory:
        """Return current AgentMemory for (user_id, agent). Bootstrap if absent."""
        ...

    def fetch_context(self, user_id: str) -> ScopeContext:
        """Fetch fresh context for this user.

        Does NOT receive Brief — weather/calendar agents produce the data that
        goes INTO Brief, so passing Brief would be circular. Brief is assembled
        by the orchestrator after all fetch_context() calls complete.
        """
        ...

    def pitch(
        self,
        brief: Brief,
        memory: AgentMemory,
        context: ScopeContext,
        user_id: str,
    ) -> list[Pitch]:
        """Return 3–5 ranked Pitches, or exactly 1 thin-signal Pitch.

        Never any other cardinality for topic-scored agents (youtube, external).
        Context agents (weather, calendar) may return 1 non-thin-signal pitch
        when their scope is inherently singular (one subject, not insufficient data).
        See agents/youtube/docs/DESIGN.md §pitch() flow.
        """
        ...

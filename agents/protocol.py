"""Shared DataAgent protocol and core data shapes.

All agents implement DataAgent. The orchestrator talks exclusively
through this interface — no agent-specific escape hatches.

Spec: agents/docs/DESIGN.md §Interface contract
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TypedDict, Protocol, runtime_checkable


# ── Today's context (assembled by orchestrator from weather + calendar) ──

class TodayContext(TypedDict):
    date: str                           # ISO 8601 date, e.g. "2026-04-16"
    day_of_week: str                    # "Monday" … "Sunday"
    time_of_day: str                    # "morning" | "afternoon" | "evening" | "night"
    weather_summary: str | None         # "rainy, 14°C" — None if weather fetch failed
    calendar_events: list[str] | None   # ["Team standup 10am"] — None if no calendar agent


# ── Brief: per-episode context object, same for all agents ──

class Brief(TypedDict):
    today_context: TodayContext


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
    agent: str                  # "youtube" | "calendar" | "weather" | "alices"
    title: str
    hook: str                   # creative brief for Producer — not spoken verbatim
    suggested_length_sec: int
    rationale: str
    source_refs: list[str]      # channel_ids / video_ids / etc.
    priority: float             # [0, 1]; higher = more important
    thin_signal: bool           # True iff exactly 1 pitch due to insufficient data
    claim_kind: str             # "durable" | "rising" | "discovery" | "neutral"
    provenance_shape: str       # "balanced" | "sub_only" | "like_only"


# ── AgentMemory: persisted per-(user, agent) state ──

class AgentMemory(TypedDict):
    schema_version: int             # = 1 for v0
    profile_state: dict             # InterestProfile; owned by agents/youtube
    topic_multiplier: dict[str, float]  # owned by learning-loop
    updated_at: str                 # ISO 8601; bumped on any field write


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
    name: str           # "youtube" | "calendar" | "weather" | "alices"
    display_name: str   # "@YouTube" | "@AlicesLens"
    scope: str          # human-readable scope description
    external: bool      # True for creator agents only
    price_usdc: float | None    # None for internal agents
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

        Never any other cardinality for topic-scored agents (youtube, alices).
        Context agents (weather, calendar) may return 1 non-thin-signal pitch
        when their scope is inherently singular (one subject, not insufficient data).
        See agents/youtube/docs/DESIGN.md §pitch() flow.
        """
        ...

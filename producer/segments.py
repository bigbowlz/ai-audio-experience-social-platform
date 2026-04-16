"""Deterministic segment selection prelude for the Producer.

Pure function — no I/O, no LLM. Selects which pitches make the
running order and assigns time budgets.

Spec: agents/docs/prompt_design.md §4
"""

from __future__ import annotations

from agents.protocol import Pitch


TARGET_EPISODE_SECS = 360
SEGUE_OVERHEAD_SECS = 10
OPEN_CLOSE_SECS = 25
MAX_SEGMENT_SEC = 90

# Per-agent default segment lengths (seconds).
# Producer owns these — agents don't set their own lengths.
# When marketplace agents arrive, this moves to DataAgent metadata.
DEFAULT_SEGMENT_SEC: dict[str, int] = {
    "youtube": 90,
    "weather": 45,
    "calendar": 30,
    "alices": 90,
}
_FALLBACK_SEGMENT_SEC = 60


def _segment_length(pitch: Pitch, overrides: dict[str, int] | None = None) -> int:
    """Resolve segment length for a pitch.

    Priority: per-call overrides > DEFAULT_SEGMENT_SEC > fallback.
    Always clamped to MAX_SEGMENT_SEC.
    """
    agent = pitch["agent"]
    if overrides and agent in overrides:
        raw = overrides[agent]
    else:
        raw = DEFAULT_SEGMENT_SEC.get(agent, _FALLBACK_SEGMENT_SEC)
    return min(raw, MAX_SEGMENT_SEC)


def select_segments(
    pitches_by_agent: dict[str, list[Pitch]],
    length_overrides: dict[str, int] | None = None,
) -> list[Pitch]:
    """Select segments for the episode running order.

    Phase 1: one guaranteed slot per agent (highest priority pitch).
    Phase 2: bonus slots from remaining pitches by priority, within budget.

    ``length_overrides`` lets the caller override the default per-agent
    segment lengths (e.g. from Producer memory or user preferences).
    """
    selected: list[Pitch] = []
    remaining: dict[str, list[Pitch]] = {}

    # Phase 1: guaranteed slot — one per agent (highest priority)
    for agent, pitches in pitches_by_agent.items():
        best = max(pitches, key=lambda p: p["priority"])
        seg_len = _segment_length(best, length_overrides)
        clamped = {**best, "suggested_length_sec": seg_len}
        selected.append(clamped)
        remaining[agent] = [p for p in pitches if p is not best]

    # Phase 2: bonus slots — highest priority across all remaining pitches
    budget = TARGET_EPISODE_SECS - OPEN_CLOSE_SECS
    budget -= sum(p["suggested_length_sec"] for p in selected)
    # N segments need N-1 segues (cold open transitions into segment 1)
    budget -= SEGUE_OVERHEAD_SECS * (len(selected) - 1)

    all_remaining = sorted(
        [p for ps in remaining.values() for p in ps],
        key=lambda p: p["priority"],
        reverse=True,
    )

    for pitch in all_remaining:
        seg_len = _segment_length(pitch, length_overrides)
        cost = seg_len + SEGUE_OVERHEAD_SECS
        if budget >= cost:
            selected.append({**pitch, "suggested_length_sec": seg_len})
            budget -= cost

    return selected

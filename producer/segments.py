"""Deterministic segment selection — Phase 1 (guaranteed slots) only.

Pure function — no I/O, no LLM. Selects one pitch per agent and computes
the seconds available for Step 1.5 (LLM bonus selection, see producer/bonus.py).

Spec: agents/docs/prompt_design.md §4 Step 1
"""

from __future__ import annotations

from agents.protocol import Pitch, RunningOrder


TARGET_EPISODE_SECS = 450
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


def select_guaranteed_slots(
    pitches_by_agent: dict[str, list[Pitch]],
    length_overrides: dict[str, int] | None = None,
) -> tuple[RunningOrder, list[Pitch], int]:
    """Phase 1 (deterministic): one guaranteed slot per agent.

    Returns:
        order: RunningOrder with `segments` = guaranteed slots only,
            `bonus_count = 0`. Step 1.5 will return an updated RunningOrder
            with bonus segments appended (see append_bonus()).
        remaining: unselected pitches with suggested_length_sec set; candidates
            for Step 1.5 (LLM bonus selection).
        budget_remaining_sec: seconds available for bonus slots, after
            open/close, guaranteed segments, and Phase 1 segues.

    Budget math:
        TARGET_EPISODE_SECS - OPEN_CLOSE_SECS
          - sum(guaranteed suggested_length_sec)
          - SEGUE_OVERHEAD_SECS * (len(guaranteed) - 1)

    The segue count is N-1 because the cold open already includes the
    transition into segment 1 (OPEN_CLOSE_SECS covers that).

    See decision 4a in docs/specs/2026-04-17-producer-alignment-plan.md.
    """
    guaranteed: list[Pitch] = []
    remaining: list[Pitch] = []

    # Sort agents deterministically so iteration order doesn't depend on dict insertion.
    for agent in sorted(pitches_by_agent):
        pitches = pitches_by_agent[agent]
        # Deterministic max: highest priority, then title ASC as final tiebreaker.
        best = min(pitches, key=lambda p: (-p["priority"], p["title"]))
        guaranteed.append(
            {**best, "suggested_length_sec": _segment_length(best, length_overrides)}
        )
        for p in pitches:
            if p is best:
                continue
            remaining.append(
                {**p, "suggested_length_sec": _segment_length(p, length_overrides)}
            )

    budget = TARGET_EPISODE_SECS - OPEN_CLOSE_SECS
    budget -= sum(p["suggested_length_sec"] for p in guaranteed)
    budget -= SEGUE_OVERHEAD_SECS * max(0, len(guaranteed) - 1)

    order: RunningOrder = {
        "segments": guaranteed,
        "total_sec": sum(p["suggested_length_sec"] for p in guaranteed),
        "guaranteed_count": len(guaranteed),
        "bonus_count": 0,
    }
    return order, remaining, budget


def append_bonus(order: RunningOrder, bonus: list[Pitch]) -> RunningOrder:
    """Pure: returns a new RunningOrder with `bonus` appended to segments."""
    new_segments = order["segments"] + bonus
    return {
        "segments": new_segments,
        "total_sec": sum(p["suggested_length_sec"] for p in new_segments),
        "guaranteed_count": order["guaranteed_count"],
        "bonus_count": len(bonus),
    }

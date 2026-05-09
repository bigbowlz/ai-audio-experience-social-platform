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
    "external": 90,
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


def collides(pitch: Pitch, seen_anchors: set[str], seen_source_refs: set[str]) -> bool:
    """True if pitch shares an anchor or any source_ref with an already-picked pitch.

    Anchor dedup is point-based (one primary id per pitch); source_refs dedup is
    set-based — two agents pulling from the same corpus can pick different anchors
    while citing overlapping supporting clips, which is the same narrative twice.
    """
    anchor = pitch.get("anchor")
    if anchor and anchor in seen_anchors:
        return True
    for ref in pitch.get("source_refs") or []:
        if ref in seen_source_refs:
            return True
    return False


def mark_seen(pitch: Pitch, seen_anchors: set[str], seen_source_refs: set[str]) -> None:
    anchor = pitch.get("anchor")
    if anchor:
        seen_anchors.add(anchor)
    for ref in pitch.get("source_refs") or []:
        seen_source_refs.add(ref)


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
    seen_anchors: set[str] = set()
    seen_source_refs: set[str] = set()

    # Sort agents deterministically so iteration order doesn't depend on dict insertion.
    for agent in sorted(pitches_by_agent):
        pitches = pitches_by_agent[agent]
        # Deterministic ordering: highest priority, then title ASC as final tiebreaker.
        ordered = sorted(pitches, key=lambda p: (-p["priority"], p["title"]))

        # Collision-aware promotion: prefer the highest-priority pitch whose
        # anchor and source_refs don't collide with an already-selected slot.
        # If every option collides, fall back to the top-priority pick rather
        # than skip the agent — guaranteed slots are about agent diversity.
        chosen: Pitch = ordered[0]
        for p in ordered:
            if collides(p, seen_anchors, seen_source_refs):
                continue
            chosen = p
            break

        mark_seen(chosen, seen_anchors, seen_source_refs)

        guaranteed.append(
            {**chosen, "suggested_length_sec": _segment_length(chosen, length_overrides)}
        )
        for p in pitches:
            if p is chosen:
                continue
            # Drop any pitch that collides with an already-accepted guaranteed
            # slot (anchor match or any shared source_ref) — its narrative
            # would duplicate a guaranteed segment. Pitches without an anchor
            # or source_refs (weather, calendar) are never deduped here.
            if collides(p, seen_anchors, seen_source_refs):
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

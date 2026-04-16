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


def select_segments(pitches_by_agent: dict[str, list[Pitch]]) -> list[Pitch]:
    """Select segments for the episode running order.

    Phase 1: one guaranteed slot per agent (highest priority pitch).
    Phase 2: bonus slots from remaining pitches by priority, within budget.
    """
    selected: list[Pitch] = []
    remaining: dict[str, list[Pitch]] = {}

    # Phase 1: guaranteed slot — one per agent (highest priority)
    for agent, pitches in pitches_by_agent.items():
        best = max(pitches, key=lambda p: p["priority"])
        clamped = {**best, "suggested_length_sec": min(best["suggested_length_sec"], MAX_SEGMENT_SEC)}
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
        clamped_len = min(pitch["suggested_length_sec"], MAX_SEGMENT_SEC)
        cost = clamped_len + SEGUE_OVERHEAD_SECS
        if budget >= cost:
            selected.append({**pitch, "suggested_length_sec": clamped_len})
            budget -= cost

    return selected

"""LLM bonus selection pass for the Producer (Step 1.5).

Runs after Phase 1 (guaranteed slots fixed) and before budget-gated bonus
inclusion. A single LLM call picks bonus slots using today_context and
claim_kind diversity. Falls back to priority-sort if the LLM is unavailable.

ProducerMemory is applied deterministically by `apply_producer_memory()`
BEFORE `select_guaranteed_slots()`; by the time pitches reach this module
the `priority` field already reflects inter-agent weights. The LLM sees
only scaled priority scalars — the raw memory dict is never in the prompt.
See producer/docs/DESIGN.md §Producer-memory learning rule.

Spec: agents/docs/prompt_design.md §4 Step 1.5
"""

from __future__ import annotations

import json
import logging
import os
from typing import TypedDict

import anthropic

from agents.protocol import Pitch, TodayContext
from producer import DEFAULT_LLM_MODEL
from producer.events import emit
from producer.prompts import BONUS_SELECTION_SYSTEM_PROMPT
from producer.segments import (
    DEFAULT_SEGMENT_SEC,
    MAX_SEGMENT_SEC,
    SEGUE_OVERHEAD_SECS,
)

_FALLBACK_SEGMENT_SEC = 60


def _segment_length(pitch: Pitch, overrides: dict[str, int] | None = None) -> int:
    """Resolve segment length for a pitch, preferring pitch["suggested_length_sec"].

    Priority: per-call overrides > pitch["suggested_length_sec"] >
    DEFAULT_SEGMENT_SEC > fallback. Always clamped to MAX_SEGMENT_SEC.

    Using pitch["suggested_length_sec"] as the primary fallback means callers
    that pre-assign lengths (e.g. select_segments()) have those lengths respected.
    """
    agent = pitch["agent"]
    if overrides and agent in overrides:
        raw = overrides[agent]
    elif "suggested_length_sec" in pitch:
        raw = pitch["suggested_length_sec"]
    else:
        raw = DEFAULT_SEGMENT_SEC.get(agent, _FALLBACK_SEGMENT_SEC)
    return min(raw, MAX_SEGMENT_SEC)


log = logging.getLogger(__name__)

MODEL = os.environ.get("PRODUCER_LLM_MODEL", DEFAULT_LLM_MODEL)
MAX_TOKENS = 1024
_TIMEOUT_SEC = 20.0
_MAX_RETRIES = 1


# ── Output types ─────────────────────────────────────────────────────


class PickReason(TypedDict):
    pitch_title: str
    agent: str
    reasoning_summary: str


class BonusPick(TypedDict):
    pitch_title: str
    agent: str
    reasoning_summary: str


class BonusSelectionResult(TypedDict):
    overall_reasoning: str
    guaranteed_pick_reasons: list[PickReason]
    bonus_picks: list[BonusPick]


# System prompt moved to producer/prompts.py (imported above).


# ── Helpers ───────────────────────────────────────────────────────────


def _format_input(
    guaranteed_slots: list[Pitch],
    remaining_pitches: list[Pitch],
    budget_remaining_sec: int,
    today_context: TodayContext,
    segue_overhead_sec: int,
) -> str:
    payload = {
        "guaranteed_slots": [
            {
                "agent": p["agent"],
                "title": p["title"],
                "priority": p["priority"],
                "claim_kind": p.get("claim_kind", "neutral"),
                "suggested_length_sec": p.get("suggested_length_sec", 90),
            }
            for p in guaranteed_slots
        ],
        "remaining_pitches": [
            {
                "agent": p["agent"],
                "title": p["title"],
                "priority": p["priority"],
                "claim_kind": p.get("claim_kind", "neutral"),
                "suggested_length_sec": p.get("suggested_length_sec", 90),
            }
            for p in remaining_pitches
        ],
        "budget_remaining_sec": budget_remaining_sec,
        "today_context": dict(today_context),
        "segue_overhead_sec": segue_overhead_sec,
    }
    return json.dumps(payload, indent=2)


def _parse_response(text: str) -> BonusSelectionResult:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3].strip()
    data = json.loads(raw)
    return BonusSelectionResult(
        overall_reasoning=data["overall_reasoning"],
        guaranteed_pick_reasons=[
            PickReason(
                pitch_title=r["pitch_title"],
                agent=r["agent"],
                reasoning_summary=r["reasoning_summary"],
            )
            for r in data["guaranteed_pick_reasons"]
        ],
        bonus_picks=[
            BonusPick(
                pitch_title=b["pitch_title"],
                agent=b["agent"],
                reasoning_summary=b["reasoning_summary"],
            )
            for b in data["bonus_picks"]
        ],
    )


def _fallback_guaranteed_reasons(guaranteed_slots: list[Pitch]) -> list[PickReason]:
    return [
        PickReason(
            pitch_title=p["title"],
            agent=p["agent"],
            reasoning_summary=f"{p['agent']}: guaranteed slot",
        )
        for p in guaranteed_slots
    ]


def _fallback_bonus_selection(
    remaining_pitches: list[Pitch],
    budget: int,
    length_overrides: dict[str, int] | None,
    segue_overhead_sec: int,
) -> list[Pitch]:
    selected: list[Pitch] = []
    # Decision 5a: deterministic across-agent tiebreaking.
    for pitch in sorted(
        remaining_pitches,
        key=lambda p: (-p["priority"], p["agent"], p["title"]),
    ):
        seg_len = _segment_length(pitch, length_overrides)
        cost = seg_len + segue_overhead_sec
        if budget >= cost:
            selected.append(
                {
                    **pitch,
                    "suggested_length_sec": seg_len,
                    "reasoning_summary": f"{pitch['agent']}: {pitch['title']}",
                }
            )
            budget -= cost
    return selected


def _find_in_remaining(title: str, remaining: list[Pitch]) -> Pitch | None:
    for p in remaining:
        if p["title"] == title:
            return p
    return None


# ── Public function ───────────────────────────────────────────────────


_FALLBACK_OVERALL_REASONING = "selecting segments by priority within time budget"


def select_bonus_segments_llm(
    guaranteed_slots: list[Pitch],
    remaining_pitches: list[Pitch],
    budget_remaining_sec: int,
    today_context: TodayContext,
    segue_overhead_sec: int = SEGUE_OVERHEAD_SECS,
    length_overrides: dict[str, int] | None = None,
) -> tuple[list[Pitch], list[PickReason], str]:
    """Pick bonus segments via LLM; fall back to priority-sort on failure.

    ProducerMemory is NOT a parameter: it has already been applied to
    `priority` upstream by apply_producer_memory(). Passing raw memory to
    the LLM would be "reasoning theater" — deterministic behavior must
    live in a pure function, not a prompt.

    Returns (bonus_pitches, guaranteed_pick_reasons, overall_reasoning).
    bonus_pitches: selected bonus pitches with suggested_length_sec and
        reasoning_summary set.
    guaranteed_pick_reasons: PickReason per guaranteed slot.
    overall_reasoning: the LLM's ≤80-char summary for the whole selection,
        forwarded verbatim to producer.selecting.started. Fallback paths
        return a fixed deterministic string per prompt_design.md:510-513.

    Note: returns plain list[Pitch] for bonus segments. Callers wrap into
    a RunningOrder via producer.segments.append_bonus().
    """
    if os.environ.get("DISABLE_LLM"):
        return (
            _fallback_bonus_selection(
                remaining_pitches,
                budget_remaining_sec,
                length_overrides,
                segue_overhead_sec,
            ),
            _fallback_guaranteed_reasons(guaranteed_slots),
            _FALLBACK_OVERALL_REASONING,
        )

    user_msg = _format_input(
        guaranteed_slots,
        remaining_pitches,
        budget_remaining_sec,
        today_context,
        segue_overhead_sec,
    )

    llm_result: BonusSelectionResult | None = None
    client = anthropic.Anthropic(max_retries=0)

    for attempt in range(1 + _MAX_RETRIES):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=BONUS_SELECTION_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
                timeout=_TIMEOUT_SEC,
            )
            if response.content and response.content[0].type == "text":
                llm_result = _parse_response(response.content[0].text)
                break
        except Exception as exc:
            log.warning("select_bonus_llm: attempt %d failed: %s", attempt + 1, exc)

    if llm_result is None:
        return (
            _fallback_bonus_selection(
                remaining_pitches,
                budget_remaining_sec,
                length_overrides,
                segue_overhead_sec,
            ),
            _fallback_guaranteed_reasons(guaranteed_slots),
            _FALLBACK_OVERALL_REASONING,
        )

    # Code-side budget enforcement — validate titles and enforce budget
    budget = budget_remaining_sec
    bonus_selected: list[Pitch] = []
    for pick in llm_result["bonus_picks"]:
        pitch = _find_in_remaining(pick["pitch_title"], remaining_pitches)
        if pitch is None:
            log.warning(
                "select_bonus_llm: unknown title %r — skipping", pick["pitch_title"]
            )
            continue
        seg_len = _segment_length(pitch, length_overrides)
        cost = seg_len + segue_overhead_sec
        if budget >= cost:
            bonus_selected.append(
                {
                    **pitch,
                    "suggested_length_sec": seg_len,
                    "reasoning_summary": pick["reasoning_summary"],
                }
            )
            budget -= cost

    return (
        bonus_selected,
        llm_result["guaranteed_pick_reasons"],
        llm_result["overall_reasoning"],
    )


def select_bonus_with_events(
    guaranteed_slots: list[Pitch],
    remaining_pitches: list[Pitch],
    budget_remaining_sec: int,
    today_context: TodayContext,
    segue_overhead_sec: int = SEGUE_OVERHEAD_SECS,
    length_overrides: dict[str, int] | None = None,
) -> tuple[list[Pitch], list[PickReason]]:
    """Same as select_bonus_segments_llm but emits Step 1.5 SSE events.

    Spec: agents/docs/prompt_design.md §4 Step 1.5 SSE integration
          producer/docs/DESIGN.md §SSE
    """
    bonus, guaranteed_reasons, overall_reasoning = select_bonus_segments_llm(
        guaranteed_slots=guaranteed_slots,
        remaining_pitches=remaining_pitches,
        budget_remaining_sec=budget_remaining_sec,
        today_context=today_context,
        segue_overhead_sec=segue_overhead_sec,
        length_overrides=length_overrides,
    )

    emit("producer.selecting.started", {"reasoning_summary": overall_reasoning})

    for slot, reason in zip(guaranteed_slots, guaranteed_reasons):
        emit("producer.pick", {
            "agent": slot["agent"],
            "pitch_title": slot["title"],
            "allocated_sec": slot["suggested_length_sec"],
            "reasoning_summary": reason["reasoning_summary"],
            "kind": "guaranteed",
        })
    for b in bonus:
        emit("producer.pick", {
            "agent": b["agent"],
            "pitch_title": b["title"],
            "allocated_sec": b["suggested_length_sec"],
            "reasoning_summary": b.get("reasoning_summary", ""),
            "kind": "bonus",
        })

    total_sec = sum(p["suggested_length_sec"] for p in guaranteed_slots + bonus)
    emit("producer.selecting.done", {
        "running_order_titles": [p["title"] for p in guaranteed_slots + bonus],
        "reasoning_summary": f"{len(guaranteed_slots) + len(bonus)} segments, {total_sec}s allocated",
    })

    return bonus, guaranteed_reasons

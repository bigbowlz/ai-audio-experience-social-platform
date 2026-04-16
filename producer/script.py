"""Producer LLM pass: EpisodeScript from selected segments + today_context.

Two-step pipeline (prompt_design.md §4):
  Step 1 (deterministic): select_segments() in producer/segments.py
  Step 2 (this module):   LLM writes full episode script

Input:  selected segments (from Step 1) + Brief.today_context
Output: EpisodeScript (cold_open, segments, sign_off)
"""

from __future__ import annotations

import json
import os
from typing import TypedDict

import anthropic

from agents.protocol import Brief, Pitch, TodayContext

# ── Output types ─────────────────────────────────────────────────────

class SegmentScript(TypedDict):
    agent: str
    pitch_title: str
    segue_in: str           # empty for first segment
    script: str
    estimated_length_sec: int


class EpisodeScript(TypedDict):
    cold_open: str
    segments: list[SegmentScript]
    sign_off: str


# ── Constants ────────────────────────────────────────────────────────

MODEL = os.environ.get("PRODUCER_LLM_MODEL", "claude-sonnet-4-20250514")
MAX_TOKENS = 8192
TARGET_EPISODE_SECS = 360

# ── System prompt ────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a radio show producer. You receive a set of selected segments \
(each with a creative hook from a domain agent) and today's context. \
Your job is to write the full episode script: a cold open, per-segment \
scripts with transitions, and a sign-off.

## Hard rules

1. **Cannot drop segments.** Every segment in the input must appear in \
   the output. You may reorder them.
2. **Cannot invent segments.** No topic or content beyond what is provided.
3. **Must produce a complete script** with:
   - Cold open (10–15s spoken, includes transition into segment 1)
   - Per-segment script (use the agent's hook as creative input, not verbatim)
   - Inter-segment segues (5–10s each, empty for the first segment)
   - Sign-off (~10s)
4. **Today's context** should be woven into cold open and segues where \
   natural. Do not force-fit weather into every segue.
5. **Segment ordering heuristics** (guidance, not hard rules):
   - Time-sensitive content first (calendar, weather)
   - Taste content after (youtube, alices)
   - Within taste: rising/discovery claim_kinds are more narratively \
     interesting as mid-show energy; durable works as a comfortable closer
6. **Respect claim_kind and provenance_shape** per segment. Do not add \
   temporal claims the agent's hook didn't make. If claim_kind is \
   "neutral", the segment script should be factual, not enthusiastic.

## Voice

Warm, conversational, like a knowledgeable friend who curates your \
listening. Not a DJ — no hype, no catchphrases. Natural pacing.

## Output format

Return a JSON object with this exact structure:
{
  "cold_open": "spoken script, 10–15s",
  "segments": [
    {
      "agent": "agent_name",
      "pitch_title": "from input",
      "segue_in": "transition from previous segment (empty string for first)",
      "script": "the spoken script for this segment",
      "estimated_length_sec": 60
    }
  ],
  "sign_off": "spoken script, ~10s"
}

Return ONLY the JSON object — no markdown fences, no commentary.
"""


# ── Input formatting ─────────────────────────────────────────────────

def _format_input(
    selected: list[Pitch],
    today_context: TodayContext,
) -> str:
    segments = []
    for p in selected:
        segments.append({
            "agent": p["agent"],
            "title": p["title"],
            "hook": p["hook"],
            "suggested_length_sec": p["suggested_length_sec"],
            "priority": p["priority"],
            "claim_kind": p.get("claim_kind", "neutral"),
            "provenance_shape": p.get("provenance_shape", "balanced"),
            "thin_signal": p.get("thin_signal", False),
        })

    payload = {
        "selected_segments": segments,
        "today_context": dict(today_context),
        "target_total_secs": TARGET_EPISODE_SECS,
    }
    return json.dumps(payload, indent=2)


# ── LLM call ─────────────────────────────────────────────────────────

def generate_episode_script(
    selected: list[Pitch],
    brief: Brief,
) -> EpisodeScript:
    """Call Claude to write the full episode script.

    Raises on LLM failure — callers decide fallback policy.
    """
    if os.environ.get("DISABLE_LLM"):
        raise RuntimeError("LLM disabled via DISABLE_LLM env var")

    client = anthropic.Anthropic()
    user_msg = _format_input(selected, brief["today_context"])

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        timeout=30.0,
    )

    if not response.content or response.content[0].type != "text":
        raise ValueError("LLM returned no text content")
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3].strip()

    data = json.loads(raw)

    # Validate structure
    segments: list[SegmentScript] = []
    for seg in data["segments"]:
        segments.append(SegmentScript(
            agent=seg["agent"],
            pitch_title=seg["pitch_title"],
            segue_in=seg.get("segue_in", ""),
            script=seg["script"],
            estimated_length_sec=seg.get("estimated_length_sec", 60),
        ))

    # Enforce "cannot drop segments" contract (prompt_design.md §4, constraint #1)
    input_keys = {(p["agent"], p["title"]) for p in selected}
    output_keys = {(s["agent"], s["pitch_title"]) for s in segments}
    missing = input_keys - output_keys
    if missing:
        agents = [a for a, _ in missing]
        raise ValueError(
            f"LLM dropped {len(missing)} segment(s) from agents: {agents}"
        )

    return EpisodeScript(
        cold_open=data["cold_open"],
        segments=segments,
        sign_off=data["sign_off"],
    )

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
TARGET_EPISODE_SECS = 450

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
   - Inter-segment segues (5–10s each)
   - Sign-off (~10s)
4. **First segment's `segue_in` is empty.** The cold open includes the \
   transition into it.
5. **Today's context** should be woven into cold open and segues where \
   natural. Do not force-fit weather into every segue.
6. **Segment ordering heuristics** (guidance, not hard rules):
   - Time-sensitive content first (calendar, weather)
   - Taste content after (youtube, alices)
   - Within taste: rising/discovery claim_kinds are more narratively \
     interesting as mid-show energy; durable works as a comfortable closer
7. **Respect claim_kind and provenance_shape per segment.** Do not add \
   temporal claims the agent's hook didn't make. If claim_kind is \
   "neutral", the segment script should be factual, not enthusiastic.

## Field legend

Each segment in `selected_segments` carries these fields:

- `agent` — source agent name; informs ordering heuristics.
- `title` — short label; must round-trip verbatim in `pitch_title`.
- `hook` — creative brief from the agent. Not spoken verbatim. For taste \
  agents, caps the phrasing you may use (see Hook vs. data layering). For \
  context agents, a one-line summary of the data.
- `rationale` — why the agent selected this topic. Context for tone; never spoken.
- `source_refs` — channel names / video titles (human-readable, NOT IDs). \
  Reference sparingly where natural ("a channel you've been subscribed to"). \
  Do not recite the full list.
- `data` — structured payload from the agent. Per-agent crib below.
- `priority`, `suggested_length_sec` — scheduling metadata, not script-level knobs.
- `claim_kind` — temporal framing permission. See claim_kind directives below.
- `provenance_shape` — evidence framing permission. Already enforced by the \
  agent in the hook; informational here.
- `thin_signal` — when `true`, the agent had insufficient personalization data.

## claim_kind directives

Each segment's `claim_kind` governs temporal framing. Do not exceed the \
permitted phrasing for the segment's claim_kind:

- **durable**: Permitted: "you've been into X", "a longtime favorite", \
  reference subscription dates. Prohibited: "lately", "recently", "getting into".
- **rising**: Permitted: "you've been getting into X lately", \
  "X is taking over your feed". Prohibited: "longtime", "always been".
- **discovery**: Permitted: "you've been exploring X", \
  "some X caught your eye recently". Prohibited: "deep into", "longtime", "always".
- **neutral**: Permitted: factual — "X showed up in your subs/likes", \
  reference specific channel/video names from `source_refs`. Prohibited: \
  any temporal or intensity claim.

## Per-agent data crib

What you'll find in `data` per agent:

- **weather** — `data.current` (temp/condition/wind), `data.day_ahead` \
  (upcoming high/low/sunset), `data.notable_facts` (top 3 ranked \
  radio-interesting facts), `data.air_quality`, `data.location_name`. \
  Ignore `hourly_forecast` and `day_past` unless surfacing a specific hour matters.
- **calendar** — `data.api_reachable` (bool), `data.events[]` with \
  `summary`, `start`, `end`, `duration_min`, `attendee_count`, `is_recurring`, \
  `has_video_call`, `organizer`.
- **youtube** / **alices** — `data` is usually `{}`. The hook + rationale \
  + source_refs are the substrate.

## Hook vs. data layering

For taste agents (`youtube`, `alices`): the hook is the phrasing ceiling. \
`claim_kind` and `provenance_shape` bound what you may claim; `data` is \
read-only context for tone calibration only. Do not combine facts from \
`data` into new temporal or intensity claims the hook did not make.

For context agents (`weather`, `calendar`): `data` is the content source. \
`hook` is a one-line safety net / summary. Prefer `data` when writing the \
segment body; use `hook` only as a fallback framing.

`provenance_shape` is already enforced by the agent when writing the hook. \
You do not need a shape table. Do not invent references to subscriptions, \
channels, or likes beyond what the hook already cites.

## thin_signal handling

When `thin_signal: true`, write a general-interest segment in the agent's \
domain — no personalization, no channels/subs/events by name. Optionally \
close with one factual sentence:

- **youtube** / **alices** — "This will get more personal as your YouTube activity grows." \
  (cause: sparse subs/likes — not actionable in the short term)
- **weather** — "Local forecast wasn't available today." (cause: location \
  skipped at generation, or forecast API failure — both opaque from the script's POV)

Keep the line factual and brief. If awkward, omit it. Never recite reasons \
across multiple segments — one per thin_signal segment, in that segment's \
own script.

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
    """Build the user-message JSON payload for Step 2.

    Carries every Pitch field per segment with safe defaults for optionals.
    No producer_memory: ProducerMemory is applied as a pure function
    upstream (see feedback_producer_memory_deterministic.md and
    docs/specs/2026-04-17-producer-step2-prompt.md §D1).
    """
    segments = []
    for p in selected:
        segments.append({
            "agent": p["agent"],
            "title": p["title"],
            "hook": p["hook"],
            "rationale": p.get("rationale", ""),
            "source_refs": p.get("source_refs", []),
            "data": p.get("data", {}),
            "priority": p["priority"],
            "claim_kind": p.get("claim_kind", "neutral"),
            "provenance_shape": p.get("provenance_shape", "balanced"),
            "thin_signal": p.get("thin_signal", False),
            "suggested_length_sec": p["suggested_length_sec"],
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

    # First segment must have empty segue_in — cold open includes the transition.
    if segments and segments[0]["segue_in"].strip():
        raise ValueError(
            f"First segment must have empty segue_in (cold open includes the "
            f"transition). Got: {segments[0]['segue_in']!r}"
        )

    return EpisodeScript(
        cold_open=data["cold_open"],
        segments=segments,
        sign_off=data["sign_off"],
    )

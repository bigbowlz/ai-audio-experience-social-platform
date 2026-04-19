"""Producer LLM pass: per-segment script generation via async iterator (Phase 3 / decision 2a).

Two-step pipeline (prompt_design.md §4):
  Step 1 (deterministic): select_segments() in producer/segments.py
  Step 2 (this module):   LLM writes per-segment scripts via stream_episode_script()

DISABLE_LLM semantics: this module raises RuntimeError on DISABLE_LLM=1 at every
entry point — there is no deterministic script fallback. (producer/bonus.py falls
back to a priority-sort when DISABLE_LLM=1; script generation has no equivalent
offline path, so callers must gate upstream as agents/orchestrator.py does via
args.no_llm.)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import AsyncIterator, TypedDict

import anthropic

from agents.protocol import Brief, Pitch
from producer import DEFAULT_LLM_MODEL, words_per_min
from producer.events import emit

# ── Output types ─────────────────────────────────────────────────────


class SegmentScript(TypedDict):
    agent: str
    pitch_title: str
    segue_in: str  # empty for first segment
    script: str
    estimated_length_sec: int


class EpisodeScript(TypedDict):
    opener: str
    segments: list[SegmentScript]
    sign_off: str


# ── Constants ────────────────────────────────────────────────────────

MODEL = os.environ.get("PRODUCER_LLM_MODEL", DEFAULT_LLM_MODEL)
SEGMENT_MAX_TOKENS = 2048
_MIN_SCRIPT_CHARS = 20
_MIN_OPENER_CHARS = 200
_SEGUE_WORD_CAP = 6

# ── System prompts ───────────────────────────────────────────────────

OPENER_SYSTEM_PROMPT = """\
You are a radio show producer writing a single ~75-second opener that fuses
warm greeting, today's weather, today's calendar shape, and a transition
into the first content segment.

One continuous spoken passage — not sectioned, not announced beat-by-beat.

## Addressing the listener

The input payload carries `user_profile`. When `user_profile.first_name` is a
non-empty string, address the listener by that first name at least once — a
natural "hey Alice" or "morning, Alice" near the opening line. When
`user_profile` is null or `first_name` is missing, address the listener as
"you". Never invent a name.

## Voice

Warm, conversational, like a knowledgeable friend. Not a DJ — no hype, no
catchphrases. Positive framing about the day's potential, but factually
objective. Do not sugarcoat bad weather or a heavy calendar. Frame opportunity
without distortion — "rain all day, good one for staying in" beats "sunshine
vibes!" when it's raining; "five meetings back-to-back" beats "action-packed
day!" when it's a grind.

## Structure (internal — do not announce)

Flow in this sequence inside one passage:
1. Greeting — addresses the listener (by first_name when present).
2. Weather beat — if `weather` input is present, surface 1–2 facts from its
   `data` field. Prefer `data.current` (temp + condition) and one entry from
   `data.day_ahead` or `data.notable_facts`. Skip entirely if `weather` is null.
3. Calendar beat — if `calendar` input is present and `data.events` is
   non-empty, describe the shape of the day — number of events, notable
   meeting, long stretches of open time. Skip if `calendar` is null, or
   mention "open day ahead" if events list is empty.
4. Transition — end with a smooth pivot into `first_content_segment`, naming
   the segment's agent lineage naturally (youtube → "your listening queue" /
   alices → "Alice's latest picks"). ≤10-word transition, not a DJ
   announcement.

## Content rules

- Never speak the labels "greeting", "weather", "calendar", "transition".
- Never announce "now for the weather" or "up next, your calendar".
- Weather and calendar are narrated as ground-truth facts about the day,
  neither listener-taste nor external-curator taste.
- Do NOT describe or summarize the first content segment beyond the micro
  transition — that segment will speak for itself.

## Pacing

Target `target_words` total for a ~`duration_sec_target`-second read at
conversational pace. Treat it as a ceiling you try to land near, not a
floor to pad toward. Short and warm beats long and padded.

Return ONLY the spoken script as plain text — no markdown fences, no JSON,
no commentary, no stage directions.
"""

SIGN_OFF_SYSTEM_PROMPT = """\
You are a radio show producer writing a ~10 second sign-off.

Warm, conversational voice. Light reference to today's context if natural;
don't force it. Close the episode — no "see you next time on [show name]"
branding since the show is unnamed. A simple, friendly close.

## Pacing

Target `target_words` total for a ~`duration_sec_target`-second read at
conversational pace. Landing short is fine; padding is not.

Return ONLY the spoken script as plain text — no markdown fences, no JSON,
no commentary, no stage directions.
"""

SYSTEM_PROMPT = """\
You are a radio show producer. You receive a single selected segment \
(with a creative hook from a domain agent) and today's context. \
Your job is to write the script for this one segment, including a segue in.

## Hard rules

1. **Cannot drop segments.** The single input segment must appear in \
   the output. pitch_title must round-trip verbatim.
2. **Cannot invent segments.** No topic or content beyond what is provided.
3. **Produce exactly one segment script** with:
   - `segue_in` (≤6 words, ~1–2s) when `is_first=false`; empty string when \
     `is_first=true`. See Segue style below.
   - `script` — the spoken script for this segment; warm, conversational tone
   - The cold open and sign-off are generated by separate calls; do not produce them here.
4. **First segment's `segue_in` is empty.** Pass-through: when `is_first=true`, \
   set segue_in to an empty string.
5. **Today's context** should be woven into the script where \
   natural. Do not force-fit weather into every segment.
6. **Segment ordering heuristics** (guidance, not hard rules):
   - Time-sensitive content first (calendar, weather)
   - Taste content after (youtube, alices)
   - Within taste: rising/discovery claim_kinds are more narratively \
     interesting as mid-show energy; durable works as a comfortable closer
7. **Respect claim_kind per segment.** Do not add temporal claims the \
   agent's hook didn't make. If claim_kind is "neutral", the segment \
   script should be factual, not enthusiastic.
8. **Pacing.** The payload carries `target_words` — the combined word \
   count for `segue_in` + `script` at conversational pace. Treat it as a \
   ceiling you try to land near. Landing short and warm is better than \
   padding to hit a word count. Set `estimated_length_sec` to your honest \
   estimate of the spoken length at the same pace; Producer measures \
   drift against it.

## Field legend

The `segment` input carries these fields:

- `agent` — source agent name; informs ordering heuristics AND provenance \
  semantics (see Per-agent provenance below).
- `title` — short label; must round-trip verbatim in `pitch_title`.
- `hook` — creative brief from the agent. Not spoken verbatim. Structured \
  WHAT/SOURCE/GOAL format for weather, calendar, and alices; prose for \
  youtube. See Hook vs. data layering.
- `source_refs` — channel names / video titles (human-readable, NOT IDs). \
  Reference sparingly where natural. Do not recite the full list.
- `data` — structured payload from the agent. Per-agent crib below.
- `claim_kind` — temporal framing permission. See claim_kind directives below.
- `thin_signal` — when `true`, the agent had insufficient personalization data.

## Per-agent provenance semantics

The `agent` field governs WHOSE taste the pitch reflects. Narration must match.

- **youtube** — provenance is the LISTENER'S own data (live OAuth). \
  Narrate as listener taste: "you've been into X", "your subscriptions".
- **alices** — provenance is an EXTERNAL CURATOR (@AlicesLens, \
  pre-captured Day-0 data). Never the listener's. Narrate as curator \
  pick: "Alice's been into X", "Alice flagged Y". Never say "you've \
  been into X" for a alices segment; the listener has no relationship \
  to this evidence.
- **weather** / **calendar** — environmental / schedule context, neither \
  taste side. Narrate as ground-truth facts about the listener's day.

## claim_kind directives

Each segment's `claim_kind` governs temporal framing. Do not exceed the \
permitted phrasing for the segment's claim_kind:

- **durable**: Permitted: "been into X for a while", "a longtime favorite", \
  reference subscription dates. Prohibited: "lately", "recently", "getting into".
- **rising**: Permitted: "been getting into X lately", \
  "X is taking over the feed". Prohibited: "longtime", "always been".
- **discovery**: Permitted: "exploring X", "X caught [their] eye recently". \
  Prohibited: "deep into", "longtime", "always".
- **neutral**: Permitted: factual — "X showed up in [their] subs/likes", \
  reference specific channel/video names from `source_refs`. Prohibited: \
  any temporal or intensity claim.

Subject pronouns follow Per-agent provenance: second person ("you") for \
youtube, third person ("Alice") for alices, none for weather/calendar.

## Per-agent data crib

What you'll find in `data` per agent:

- **weather** — `data.current` (temp/condition/wind), `data.day_ahead` \
  (upcoming high/low/sunset), `data.notable_facts` (top 3 ranked \
  radio-interesting facts), `data.air_quality`, `data.location_name`. \
  Ignore `day_past` unless surfacing a specific hour matters.
- **calendar** — `data.api_reachable` (bool), `data.events[]` with \
  `summary`, `start`, `end`, `duration_min`, `attendee_count`, `is_recurring`, \
  `has_video_call`, `organizer`.
- **youtube** / **alices** — `data` is usually `{}`. The hook + \
  source_refs are the substrate.

## Hook vs. data layering

For taste agents (`youtube`, `alices`): the hook is the phrasing ceiling. \
`claim_kind` bounds what you may claim; `data` is read-only context for \
tone calibration only. Do not combine facts from `data` into new temporal \
or intensity claims the hook did not make.

For context agents (`weather`, `calendar`): the hook is a structured \
WHAT/SOURCE/GOAL brief orienting you; `data` is the content source. Write \
the segment body from `data`; the hook is orientation, not narration \
material. Never speak the WHAT/SOURCE/GOAL labels on-air.

## thin_signal handling

When `thin_signal: true`, write a general-interest segment in the agent's \
domain — no personalization, no channels/subs/events by name. Optionally \
close with one factual sentence:

- **youtube** — "This will get more personal as your YouTube activity grows." \
  (cause: sparse subs/likes — not actionable in the short term)
- **alices** — omit the one-sentence close; Alice's data is fixed Day-0 \
  and won't grow.
- **weather** — "Local forecast wasn't available today." (cause: location \
  skipped at generation, or forecast API failure — both opaque from the script's POV)

Keep the line factual and brief. If awkward, omit it. Never recite reasons \
across multiple segments — one per thin_signal segment, in that segment's \
own script.

## Voice

Warm, conversational, like a knowledgeable friend who curates your \
listening. Not a DJ — no hype, no catchphrases. Natural pacing.

## Segue style

When `is_first=false`, `segue_in` is a micro-bridge — a single conjunction \
or short connector linking the previous segment to this one. Target ≤6 \
words (~1–2 seconds spoken). Never a full sentence, never a DJ-style \
announcement of what's coming next.

Examples: "Meanwhile,", "Speaking of which —", "On a different note,", \
"From that to —", "Now,", "And —".

Empty string is allowed when the transition is self-evident (for example, \
pivoting from weather to a time-sensitive calendar event). Do not pad \
for transition's sake. `segue_in` is NOT counted against `target_words` \
and must not eat into the segment's spoken budget.

## Output format

Return a JSON object for this single segment with exactly these keys:
{
  "agent": "agent_name (same as input)",
  "pitch_title": "from input — must round-trip verbatim",
  "segue_in": "micro-bridge from previous segment, ≤6 words (empty when is_first=true or transition is self-evident)",
  "script": "the spoken script for this segment",
  "estimated_length_sec": 60
}

Return ONLY the JSON object — no markdown fences, no commentary.
"""


# ── LLM call ─────────────────────────────────────────────────────────

_client = anthropic.Anthropic()


def _target_words(duration_sec: int, wpm: int | None = None) -> int:
    """Words-per-minute → words for a given spoken duration.

    Rounded to the nearest int, floor 1. Used both in LLM payloads (as
    the target the model aims for) and in post-hoc drift measurement
    (words → seconds via the inverse).
    """
    effective_wpm = wpm if wpm is not None else words_per_min()
    return max(1, round(duration_sec * effective_wpm / 60))


def _words_to_sec(word_count: int, wpm: int | None = None) -> float:
    """Inverse of _target_words: spoken words → estimated seconds at WPM."""
    effective_wpm = wpm if wpm is not None else words_per_min()
    return word_count * 60 / effective_wpm


_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def _slug_title(title: str) -> str:
    """Normalize a pitch title into a filesystem-safe slug.

    Lowercased; non-alphanumerics collapsed to a single underscore; leading and
    trailing underscores stripped. Non-ASCII characters fall through the
    non-alphanumeric class (they become underscores), which is fine for dev
    probe data in the v0 cache. Never returns empty — an all-punctuation title
    yields '_' so the filename stays well-formed.
    """
    slug = _SLUG_PATTERN.sub("_", title.lower()).strip("_")
    return slug or "_"


def _segment_cache_path(agent: str, title: str, date: str, wpm: int) -> Path:
    """Cache file path for a (agent, title, date, wpm) key.

    Returns `<cache_dir>/segment_scripts/{agent}_{slug}_{YYYYMMDD}_{wpm}.json`.
    Date dashes are stripped so the filename stays short and ls-sortable.
    """
    from producer import cache_dir
    date_compact = date.replace("-", "")
    slug = _slug_title(title)
    return cache_dir() / "segment_scripts" / f"{agent}_{slug}_{date_compact}_{wpm}.json"


async def generate_segment(
    segment: Pitch,
    brief: Brief,
    is_first: bool,
) -> SegmentScript:
    """Single async LLM call producing one SegmentScript.

    Enforces first-segue-empty and script-length-floor internally.
    No events emitted here — stream_episode_script handles SSE.
    """
    if os.environ.get("DISABLE_LLM"):
        raise RuntimeError("LLM disabled via DISABLE_LLM env var")
    payload = {
        "segment": {
            "agent": segment["agent"],
            "title": segment["title"],
            "hook": segment["hook"],
            "source_refs": segment.get("source_refs", []),
            "data": segment.get("data", {}),
            "claim_kind": segment.get("claim_kind", "neutral"),
            "thin_signal": segment.get("thin_signal", False),
        },
        "today_context": dict(brief["today_context"]),
        "is_first": is_first,
        "target_words": _target_words(segment["suggested_length_sec"]),
        "words_per_minute": words_per_min(),
    }
    user_msg = json.dumps(payload, indent=2)

    response = await asyncio.to_thread(
        _client.messages.create,
        model=MODEL,
        max_tokens=SEGMENT_MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
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

    seg = SegmentScript(
        agent=data["agent"],
        pitch_title=data["pitch_title"],
        segue_in=data.get("segue_in", ""),
        script=data["script"],
        estimated_length_sec=data.get("estimated_length_sec", 60),
    )

    if is_first and seg["segue_in"].strip():
        raise ValueError(
            f"First segment must have empty segue_in. Got: {seg['segue_in']!r}"
        )

    if len(seg["script"].strip()) < _MIN_SCRIPT_CHARS:
        raise ValueError(
            f"Segment ({seg['agent']}/{seg['pitch_title']}) script too short: "
            f"{len(seg['script'])} chars (min {_MIN_SCRIPT_CHARS})"
        )

    if not is_first:
        segue_words = len(seg["segue_in"].split())
        if segue_words > _SEGUE_WORD_CAP:
            print(
                f"[producer.script] segue over cap "
                f"({segue_words} words > {_SEGUE_WORD_CAP}): "
                f"{seg['agent']}/{seg['pitch_title']!r} segue_in={seg['segue_in']!r}",
                file=sys.stderr,
            )

    return seg


async def stream_episode_script(
    selected: list[Pitch],
    brief: Brief,
) -> AsyncIterator[SegmentScript]:
    """Async generator yielding one SegmentScript per input pitch.

    Emits script.segment.done SSE per segment.
    Enforces cannot-drop-segments at end.
    Re-enforces script-length floor on each segment (defense in depth).
    """
    input_keys = {(p["agent"], p["title"]) for p in selected}
    output_keys: set[tuple[str, str]] = set()

    for i, pitch in enumerate(selected):
        seg = await generate_segment(pitch, brief, is_first=(i == 0))

        if i == 0 and seg["segue_in"].strip():
            raise ValueError(
                f"First segment must have empty segue_in. Got: {seg['segue_in']!r}"
            )

        if len(seg["script"].strip()) < _MIN_SCRIPT_CHARS:
            raise ValueError(
                f"Segment ({seg['agent']}/{seg['pitch_title']}) script too short: "
                f"{len(seg['script'])} chars (min {_MIN_SCRIPT_CHARS})"
            )

        emit("script.segment.done", {
            "index": i,
            "agent": seg["agent"],
            "pitch_title": seg["pitch_title"],
        })

        # Pacing telemetry — measured vs. target drift. Option-A (prompt-only)
        # pacing enforcement: no retry loop, just instrumentation so a later
        # escalation to retry has real drift numbers behind it.
        wpm = words_per_min()
        target_sec = pitch["suggested_length_sec"]
        spoken_words = len(
            (seg["segue_in"] + " " + seg["script"]).split()
        )
        measured_sec = _words_to_sec(spoken_words, wpm)
        target_words = _target_words(target_sec, wpm)
        emit("producer.segment.pacing_measured", {
            "index": i,
            "agent": seg["agent"],
            "pitch_title": seg["pitch_title"],
            "target_sec": target_sec,
            "target_words": target_words,
            "words": spoken_words,
            "measured_sec": round(measured_sec, 1),
            "estimated_sec_self_report": seg["estimated_length_sec"],
            "drift_sec": round(measured_sec - target_sec, 1),
            "words_per_minute": wpm,
        })

        output_keys.add((seg["agent"], seg["pitch_title"]))
        yield seg

    missing = input_keys - output_keys
    if missing:
        agents = [a for a, _ in missing]
        raise ValueError(f"LLM dropped {len(missing)} segment(s) from agents: {agents}")


_OPENER_DURATION_SEC = 75
_SIGN_OFF_DURATION_SEC = 10


def _opener_pitch_payload(pitch: Pitch | None) -> dict | None:
    """Shape a weather or calendar Pitch for the opener payload.

    None passes through as None so the prompt can conditionally skip the beat.
    """
    if pitch is None:
        return None
    return {
        "agent": pitch["agent"],
        "title": pitch["title"],
        "hook": pitch["hook"],
        "data": pitch.get("data", {}),
        "source_refs": pitch.get("source_refs", []),
        "thin_signal": pitch.get("thin_signal", False),
    }


def split_opener_inputs(
    selected: list[Pitch],
) -> tuple[Pitch | None, Pitch | None, list[Pitch]]:
    """Split the running order into (weather, calendar, content_pitches).

    Weather and calendar are fused into the single LLM opener call; everything
    else is passed to stream_episode_script as-is. Ordering within each bucket
    is preserved from `selected`.
    """
    weather = next((p for p in selected if p["agent"] == "weather"), None)
    calendar = next((p for p in selected if p["agent"] == "calendar"), None)
    content = [p for p in selected if p["agent"] not in ("weather", "calendar")]
    return weather, calendar, content


async def generate_opener(
    weather_pitch: Pitch | None,
    calendar_pitch: Pitch | None,
    first_content_pitch: Pitch,
    brief: Brief,
) -> str:
    """Single LLM call: fused ~75s opener (greeting + weather + calendar + transition).

    Replaces the separate cold_open + weather-segment + calendar-segment trio.
    Weather and/or calendar inputs may be None; the prompt degrades gracefully
    (skips the absent beat). Always ends with a transition into
    `first_content_pitch`. Addresses the user by first name when
    `brief.user_profile.first_name` is present, falling back to "you".
    """
    if os.environ.get("DISABLE_LLM"):
        raise RuntimeError("LLM disabled via DISABLE_LLM env var")
    user_profile = brief.get("user_profile")
    payload = {
        "task": "opener",
        "weather": _opener_pitch_payload(weather_pitch),
        "calendar": _opener_pitch_payload(calendar_pitch),
        "first_content_segment": {
            "agent": first_content_pitch["agent"],
            "title": first_content_pitch["title"],
            "hook": first_content_pitch["hook"],
        },
        "today_context": dict(brief["today_context"]),
        "user_profile": dict(user_profile) if user_profile else None,
        "duration_sec_target": _OPENER_DURATION_SEC,
        "target_words": _target_words(_OPENER_DURATION_SEC),
        "words_per_minute": words_per_min(),
    }
    response = await asyncio.to_thread(
        _client.messages.create,
        model=MODEL, max_tokens=800, system=OPENER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
        timeout=20.0,
    )
    if not response.content or response.content[0].type != "text":
        raise ValueError("LLM returned no text content")
    text = response.content[0].text.strip()
    if len(text) < _MIN_OPENER_CHARS:
        raise ValueError(
            f"Opener script too short: {len(text)} chars (min {_MIN_OPENER_CHARS})"
        )
    return text


async def generate_sign_off(brief: Brief) -> str:
    """LLM call: ~10s spoken sign-off."""
    if os.environ.get("DISABLE_LLM"):
        raise RuntimeError("LLM disabled via DISABLE_LLM env var")
    payload = {
        "task": "sign_off",
        "today_context": dict(brief["today_context"]),
        "duration_sec_target": _SIGN_OFF_DURATION_SEC,
        "target_words": _target_words(_SIGN_OFF_DURATION_SEC),
        "words_per_minute": words_per_min(),
    }
    response = await asyncio.to_thread(
        _client.messages.create,
        model=MODEL, max_tokens=200, system=SIGN_OFF_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
        timeout=10.0,
    )
    if not response.content or response.content[0].type != "text":
        raise ValueError("LLM returned no text content")
    return response.content[0].text.strip()


def generate_episode_script(
    selected: list[Pitch],
    brief: Brief,
) -> EpisodeScript:
    """Sync back-compat collector. Splits weather/calendar out into the fused
    opener, streams content segments, and runs sign_off.
    """
    if os.environ.get("DISABLE_LLM"):
        raise RuntimeError("LLM disabled via DISABLE_LLM env var")
    weather_pitch, calendar_pitch, content_pitches = split_opener_inputs(selected)
    if not content_pitches:
        raise ValueError(
            "generate_episode_script: no content pitches after opener split "
            "(running order was weather/calendar only)"
        )

    async def _collect() -> EpisodeScript:
        opener = await generate_opener(
            weather_pitch, calendar_pitch, content_pitches[0], brief
        )
        segments: list[SegmentScript] = [
            seg async for seg in stream_episode_script(content_pitches, brief)
        ]
        sign_off = await generate_sign_off(brief)
        return EpisodeScript(opener=opener, segments=segments, sign_off=sign_off)

    return asyncio.run(_collect())

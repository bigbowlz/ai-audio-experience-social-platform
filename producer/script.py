"""Producer LLM pass: per-segment script generation via async iterator (Phase 3 / decision 2a).

Two-step pipeline (prompt_design.md §4):
  Step 1 (deterministic): select_segments() in producer/segments.py
  Step 2 (this module):   LLM writes per-segment scripts via stream_episode_script()

DISABLE_LLM semantics: this module raises RuntimeError on DISABLE_LLM=1 at every
entry point — there is no deterministic script fallback. Research-based
narration for youtube/alices segments is LLM-only by construction (the model
calls the web_search tool inside the same messages.create call); there is no
offline equivalent, so callers must gate upstream as agents/orchestrator.py
does via args.no_llm. See docs/specs/2026-04-18-producer-news-narration-design.md.
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
You are a radio show producer writing a single warm opener that fuses
greeting, today's weather, today's calendar shape, and a transition
into the first content segment.

One continuous spoken passage — not sectioned, not announced beat-by-beat.

## Addressing the listener

The input payload carries `user_profile`. When `user_profile.first_name` is a
non-empty string, address the listener by that first name at least once — a
natural "hey FIRST_NAME" or "morning, FIRST_NAME" near the opening line. When
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
   non-empty, describe the shape of the window — number of events, notable
   meeting naming the attendees(if <=3 in total), stretches between events inside the window. Skip if `calendar`
   is null.

## Calendar window awareness

The `calendar.data.events` list is a **rolling 16-hour window** starting from
`today_context.now` (24-hour local time, HH:MM:SS). Events later than `now + 16h` are
NOT in the list. Describe the shape of what IS in the window ("a one-on-one at 15:45,
then a clear stretch after"). Do NOT characterize blocks you haven't seen
("evening is open", "rest of the day is clear", "nothing else on the
books", "just the one thing") unless they fall inside the window. When the
events list is empty, say "nothing on the immediate horizon" — not "open
day".
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

Target {target_words} words total at conversational pace. Treat the word
count as a ceiling you try to land near, not a floor to pad toward. Short
and warm beats long and padded.

## Bilingual handling

The listener reads and hears English and Chinese fluently, and recognises
some Japanese. When any Chinese or Japanese proper noun, title, phrase, or
quote comes up, keep it in its original script — no translation, no
pinyin / romaji, no parenthetical glosses. English narration around it is
fine.

Never emit `<cite>`, `<br>`, or any other inline HTML / XML markup — the
spoken script is plain text, and any tags make it into the audio as
garbage. Inline citation markers from web_search results must be dropped,
not repeated.

Return ONLY the spoken script as plain text — no markdown fences, no JSON,
no commentary, no stage directions.
"""

SIGN_OFF_SYSTEM_PROMPT = """\
You are a radio show producer writing a brief sign-off.

Warm, conversational voice. Close the episode clearly so the listener
knows their personalized feed for today is over.

## Structure (internal — do not announce labels)

Two beats, one continuous passage:

1. **Close beat (~70%)** — one sentence that signals this was today's
   personalized feed / picks for today. Phrase it naturally, varying
   day-to-day — "that's today's feed", "that wraps your picks for today",
   "that's the show for today". A soft reference to today's weather or
   calendar context is allowed if it lands cleanly; do not force it.
2. **Parting line (~30%)** — a short, warm sign-off. No "see you next
   time on [show name]" branding — the show is unnamed.

Avoid the word "podcast" as a noun on-air; say "today's feed", "your
picks for today", or "today's show" instead. Never say "episode" on-air
either.

## Pacing

Target {target_words} words total at conversational pace. Landing short
is fine; padding is not.

## Bilingual handling

The listener reads and hears English and Chinese fluently, and recognises
some Japanese. When any Chinese or Japanese proper noun, title, phrase, or
quote comes up, keep it in its original script — no translation, no
pinyin / romaji, no parenthetical glosses.

Never emit `<cite>`, `<br>`, or any other inline HTML / XML markup — the
spoken script is plain text, and any tags make it into the audio as
garbage. Inline citation markers from web_search results must be dropped,
not repeated.

Return ONLY the spoken script as plain text — no markdown fences, no JSON,
no commentary, no stage directions.
"""

SYSTEM_PROMPT = """\
You produce a personalized daily podcast — a short spoken show built fresh \
each day for one specific listener. "Personalized" means the episode is \
assembled from signals tied to that listener: their YouTube subscriptions \
and likes (@youtube), an external curator they've opted into (@alices, \
Alice's taste), and today's weather and calendar. Different listeners \
receive different episodes; the same listener hears a different episode \
tomorrow because the underlying signals shift day-to-day.

You receive a single selected segment (with a creative hook from a domain \
agent) and today's context. Your job is to write the script for this one \
segment, including a segue in.

## Segment kinds

- **youtube** and **alices** segments are TASTE segments. You use the \
  `web_search` tool to find a real-world story or news item in the pitch's \
  topic area, and narrate that story. The pitch's hook / data / source_refs \
  are topical anchors — they shape WHAT area to search, they are not narration \
  material.
- **weather** and **calendar** segments do NOT use web_search; they narrate \
  directly from the pitch's `data` field, same structured contract as before. \
  Note: in this v0, weather and calendar are fused into the separate opener \
  prompt, so this path typically receives youtube / alices only.

## Hard rules

1. **Cannot drop segments.** The single input segment must appear in \
   the output. pitch_title must round-trip verbatim.
2. **Cannot invent segments.** No topic or content beyond what is provided.
3. **Produce exactly one segment script** with:
   - `segue_in` (≤6 words, ~1–2s) when `is_first=false`; empty string when \
     `is_first=true`. See Segue style below.
   - `script` — the spoken script for this segment; warm, conversational tone
   - The opener and sign-off are generated by separate calls; do not produce them here.
4. **First segment's `segue_in` is empty.** Pass-through: when `is_first=true`, \
   set segue_in to an empty string.
5. **Today's context** should be woven into the script where \
   natural. Do not force-fit weather into every segment.
6. **Respect claim_kind per segment.** Do not add temporal claims the \
   agent's hook didn't make. If claim_kind is "neutral", the segment \
   script should be factual, not enthusiastic. claim_kind directives bound \
   the TAKEAWAY's temporal framing (see claim_kind directives below).
7. **Pacing.** The payload carries `target_words` — the combined word \
   count for `segue_in` + `script` at conversational pace. Treat it as a \
   ceiling you try to land near. Landing short and warm is better than \
   padding to hit a word count. Set `estimated_length_sec` to your honest \
   estimate of the spoken length at the same pace; Producer measures \
   drift against it.

## Research via web_search (taste segments)

For `youtube` and `alices` segments, you have the `web_search` tool \
available. You MAY use it up to 2 times per segment.

Find a real-world angle on the topic — a current news item, a historical \
or backstory fact, a notable cultural moment, or a concrete detail about \
the people, places, or works involved. Pick whichever is most interesting \
today; don't force a news peg. Older or evergreen material is welcome \
when it's the strongest angle on the topic.

**Query derivation:**
- Derive your search query from the pitch's `title` field. Don't append \
  today's date unless freshness is specifically what makes the angle \
  interesting.
- NEVER include the listener's channel names, video titles, or any proper \
  nouns from `source_refs` in the search query. `source_refs` are listener \
  data — they stay out of search input.
- Prefer short, topical queries: `"underwater photography"` beats \
  `"National Geographic underwater photography documentary site:nationalgeographic.com"`.

**Primary search + broadened retry:**
1. Issue one query derived from `title`.
2. If the primary search returns nothing usable as an angle, issue ONE \
   broadened retry — drop a "news" qualifier if you added one, or climb to \
   a parent topic (e.g., `"underwater photography news"` → `"photography"`).
3. Do not issue more than 2 searches total.

**Generic-trend failure (counts as nothing-usable):**
If the search returns only sweeping trend-piece content — "audiences are \
embracing X", "several forces are aligning", "fans are creating", "what \
sets modern X apart" — with no named people, works, dates, places, or \
numbers, treat that as nothing usable and fall back (broadened retry, \
then hook-narration). A segment with real named facts from the hook \
beats a segment made of industry-think-piece vapor.

**Hook-narration fallback:**
If both searches come back empty or nothing is usable as an angle, fall back \
to narrating from the pitch `hook` / `source_refs` / `data` in the data-pattern \
voice — the pre-research behavior. The segment still airs (the system cannot \
drop segments in v0).

## Narration contract (taste segments)

Internal beats — never announced, never labeled. One continuous passage.

- **Segue in** — `segue_in` field, ≤6 words. Micro-bridge from the previous \
  segment. See Segue style.
- **Lead** (~10% of `target_words`) — drop straight into the angle: \
  who, where, what. One or two sentences. NO "here's a story about X" \
  announcement. NO "this week in photography…" framing.
- **Factual body** (~70%) — concrete facts from research: named people, \
  works, places, dates, numbers, quotes, causes and effects. At least four \
  distinct factual sentences. Commentary and emotional adjectives stay out \
  of this band.
- **Flex band** (~10%) — one short passage for flow. May be either another \
  fact, a vivid detail, or a single line of commentary / emotional framing \
  that smooths the transition into the takeaway. Use judgment on which the \
  segment needs more.
- **Takeaway** (~10%) — one sentence that lands the segment. An IMPLICIT \
  tie to the listener's domain is permitted (e.g., "the kind of story that \
  travels well in photography circles"). An EXPLICIT BRIDGE is forbidden — \
  never "because you watched X, here's Y", never "since you've been into \
  X…". claim_kind directives still bound temporal framing in the takeaway.

**Source-recitation rule (critical):** the listener's channel names, video \
titles, and any `source_refs` proper nouns are NOT spoken anywhere in the \
story body. The pitch's topic is the shared ground between the listener and \
the story — the listener's data is NOT. You MAY use `source_refs` as context \
to avoid coincidental overlap (e.g., don't pick a story about the exact \
channel the listener already watches), but you MUST NOT recite those names \
on-air.

## Bilingual handling

The listener reads and hears English and Chinese fluently, and recognises \
some Japanese. When any Chinese or Japanese proper noun, title, phrase, or \
quote comes up, keep it in its original script — no translation, no \
pinyin / romaji, no parenthetical glosses. English narration around it is \
fine. This applies equally to song / film / album titles and to personal \
or place names. Other non-English languages are out of scope; translate \
or romanize those as you normally would.

## Field legend

The `segment` input carries these fields:

- `agent` — source agent name; informs ordering heuristics AND provenance \
  semantics (see Per-agent provenance below).
- `title` — short label; must round-trip verbatim in `pitch_title`. Also the \
  search-query seed for taste segments.
- `hook` — creative brief from the agent. Not spoken verbatim. Structured \
  WHAT/SOURCE/GOAL format for weather, calendar, and alices; prose for \
  youtube. Topical anchor for the story search.
- `source_refs` — channel names / video titles (human-readable, NOT IDs). \
  Context for disambiguation only. NOT spoken, NOT in the search query.
- `data` — structured payload from the agent. Per-agent crib below.
- `claim_kind` — temporal framing permission in the takeaway.
- `thin_signal` — when `true`, the agent had insufficient personalization data.

## Per-agent provenance semantics

The `agent` field governs WHOSE taste the pitch reflects. The story body is \
third-party news either way; provenance only colors the TAKEAWAY voice.

- **youtube** — provenance is the LISTENER'S own data. Takeaway MAY use \
  second person sparingly — "the kind of story that rewards the \
  underwater-photography crowd". Never "you've been into X" style.
- **alices** — provenance is an EXTERNAL CURATOR (@GoddamnAxl, \
  pre-captured Day-0 data). Takeaway uses third person — "Alice" or \
  "Alice's lens" — never "you" about curator taste. **Takeaway focus \
  rule:** the takeaway addresses the LISTENER — what's worth the listener's \
  attention in this angle. Do NOT speculate about Alice's motivations, \
  arc, identity, or what "fits Alice's lens / radar / taste tree". \
  Alice's name appears once in the takeaway at most, as curator \
  attribution only. The segment is the listener's daily feed; Alice is \
  a sourced voice inside it, not the subject.
- **weather** / **calendar** — environmental / schedule context. Narrated \
  directly from `data` (no web_search). Typically routed through the separate \
  opener prompt.

## claim_kind directives

Each segment's `claim_kind` governs temporal framing in the takeaway. Do not \
exceed the permitted phrasing for the segment's claim_kind:

- **durable**: Permitted: "been into X for a while", "a longtime favorite". \
  Prohibited: "lately", "recently", "getting into".
- **rising**: Permitted: "been getting into X lately", \
  "X is taking over the feed". Prohibited: "longtime", "always been".
- **discovery**: Permitted: "exploring X", "X caught [their] eye recently". \
  Prohibited: "deep into", "longtime", "always".
- **neutral**: Permitted: factual framing — "X showed up in [their] activity". \
  Prohibited: any temporal or intensity claim.

Subject pronouns follow Per-agent provenance: second person ("you") for \
youtube sparingly, third person ("Alice") for alices, none for weather/calendar.

## Per-agent data crib

What you'll find in `data` per agent:

- **weather** — `data.current` (temp/condition/wind), `data.day_ahead` \
  (upcoming high/low/sunset), `data.notable_facts` (top 3 ranked \
  radio-interesting facts), `data.air_quality`, `data.location_name`.
- **calendar** — `data.api_reachable` (bool), `data.events[]` with \
  `summary`, `start`, `end`, `duration_min`, `attendee_count`, `attendees` \
  (list of display names; may be shorter than `attendee_count` or empty when \
  names aren't resolvable — never contains emails), `is_recurring`, \
  `has_video_call`, `organizer`.
- **youtube** / **alices** — `data` is usually `{}`. The hook + \
  source_refs are the substrate. For taste segments in the research path, \
  `data` is read-only context for tone calibration only.

## Hook vs. data layering

For taste agents (`youtube`, `alices`): the hook is the phrasing ceiling. \
`claim_kind` bounds what you may claim in the takeaway; `data` is \
read-only context for tone calibration only. Do not combine facts from `data` \
into new temporal or intensity claims the hook did not make.

For context agents (`weather`, `calendar`): the hook is a structured \
WHAT/SOURCE/GOAL brief orienting you; `data` is the content source. Write \
the segment body from `data`; the hook is orientation, not narration \
material. Never speak the WHAT/SOURCE/GOAL labels on-air.

## thin_signal handling

When `thin_signal: true`, write a general-interest segment in the agent's \
domain — no personalization, no channels/subs/events by name. Optionally \
close with one factual sentence:

- **youtube** — "This will get more personal as your YouTube activity grows."
- **alices** — omit the one-sentence close; Alice's data is fixed Day-0 \
  and won't grow.
- **weather** — "Local forecast wasn't available today."

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

Empty string is allowed when the transition is self-evident. Do not pad \
for transition's sake. `segue_in` is NOT counted against `target_words` \
and must not eat into the segment's spoken budget.

## Output format

Return a JSON object for this single segment with exactly these keys:
{
  "agent": "agent_name (same as input)",
  "pitch_title": "from input — must round-trip verbatim",
  "segue_in": "micro-bridge from previous segment, ≤6 words (empty when is_first=true)",
  "script": "the spoken script for this segment",
  "estimated_length_sec": 60
}

## JSON safety rules

The `script` and `segue_in` values are string fields in a JSON object. Invalid \
JSON breaks the pipeline. Follow these rules every time:

- Any `"` character inside a string value MUST be escaped as `\\"`.
- Any newline inside a string value MUST be escaped as `\\n` — never a raw \
  line break mid-string.
- Any backslash inside a string value MUST be doubled as `\\\\`.
- Prefer narration without quoted phrases. If you must quote something from \
  research — a headline, a person's words, a song title — use single quotes \
  (`'like this'`) or em-dashes (`— like this —`) instead of double quotes, so \
  escaping never becomes an issue.

## No inline markup in the script

The `script` and `segue_in` string values are spoken aloud by TTS. Never \
emit `<cite>`, `<br>`, or any other inline HTML / XML tags inside them. \
Inline citation markers from web_search results (e.g. `<cite index="7-21">`) \
MUST be dropped, not repeated — narrate the fact in your own words. Any tag \
that survives into the script gets read as garbage audio.

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


_CITE_TAG = re.compile(r"<cite\b[^>]*>(.*?)</cite>", re.DOTALL | re.IGNORECASE)
_BR_TAG = re.compile(r"<br\s*/?>", re.IGNORECASE)
_MULTI_WS = re.compile(r"[ \t]{2,}")


def _strip_inline_markup(text: str) -> str:
    """Strip `<cite>` / `<br>` tags from LLM output.

    Web-search responses frequently leak `<cite index="...">...</cite>`
    citation markers; some models also emit `<br>`. Both are unspeakable
    and would mangle TTS. Content inside `<cite>` is preserved; `<br>`
    becomes a space. Adjacent whitespace is collapsed so the removal
    doesn't leave double spaces mid-line.
    """
    text = _CITE_TAG.sub(r"\1", text)
    text = _BR_TAG.sub(" ", text)
    text = _MULTI_WS.sub(" ", text)
    return text


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


_SEGMENT_REQUIRED_KEYS = {
    "agent",
    "pitch_title",
    "segue_in",
    "script",
    "estimated_length_sec",
}


def _read_cached_segment(path: Path) -> SegmentScript | None:
    """Return `artifact["segment"]` on hit, or None on any parse/IO failure.

    Soft-fail contract (spec §3): a corrupted or malformed cache file is logged
    and treated as a miss. Missing required keys → miss. The aim is honest
    degradation — an unreadable cache never blocks generation.
    """
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        segment = data["segment"]
        missing = _SEGMENT_REQUIRED_KEYS - set(segment)
        if missing:
            raise KeyError(f"missing segment keys: {sorted(missing)}")
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        print(
            f"[producer.script] cache read failed for {path}: {exc!r} — treating as miss",
            file=sys.stderr,
        )
        return None
    return SegmentScript(
        agent=segment["agent"],
        pitch_title=segment["pitch_title"],
        segue_in=_strip_inline_markup(segment["segue_in"]),
        script=_strip_inline_markup(segment["script"]),
        estimated_length_sec=segment["estimated_length_sec"],
    )


def _write_cached_artifact(path: Path, segment: SegmentScript, debug: dict) -> None:
    """Atomically write `{segment, debug}` as pretty-printed JSON.

    Writes to `<path>.tmp` in the same directory, then `os.replace()` onto the
    final path so readers never see a partial file. Parent directories are
    created on demand. Cleans up the tmp file on any write failure.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {"segment": dict(segment), "debug": debug}
    try:
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, path)
    except OSError:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def _extract_segment_text(response: object) -> str:
    """Return the final `text` content block's text, after any tool blocks.

    Anthropic returns a heterogeneous content list when tools are used:
    `server_tool_use`, `web_search_tool_result`, then `text`. We want the last
    `text` block (the model's final answer). Raises ValueError when no text
    block is present.
    """
    content = getattr(response, "content", None) or []
    text_blocks = [b for b in content if getattr(b, "type", None) == "text"]
    if not text_blocks:
        raise ValueError("LLM returned no text content")
    return text_blocks[-1].text.strip()


def _count_web_search_uses(response: object) -> int:
    """Number of `server_tool_use` blocks for `web_search` on the response.

    Observed ground truth — contrast with the prior LLM-self-reported
    `research_outcome` field, which is gone. `0` means the model never searched;
    callers emit `producer.segment.research_fallback` in that case.
    """
    content = getattr(response, "content", None) or []
    return sum(
        1
        for b in content
        if getattr(b, "type", None) == "server_tool_use"
        and getattr(b, "name", None) == "web_search"
    )


def _extract_search_queries(response: object) -> list[str]:
    """Queries the model actually sent to web_search, in order.

    Read from each `server_tool_use` block's `input.query`. Anything missing or
    non-string is skipped. Used for debug artifacts so we can audit the model's
    search behavior after the fact.
    """
    content = getattr(response, "content", None) or []
    queries: list[str] = []
    for b in content:
        if (
            getattr(b, "type", None) == "server_tool_use"
            and getattr(b, "name", None) == "web_search"
        ):
            inp = getattr(b, "input", None)
            if isinstance(inp, dict):
                q = inp.get("query")
                if isinstance(q, str) and q:
                    queries.append(q)
    return queries


def _extract_json_object(text: str) -> str:
    """Return the outermost balanced `{...}` substring, ignoring string bodies.

    Tolerates prose wrappers the model sometimes emits around the JSON ("Here's
    the output:\\n{...}") and trailing commentary. Scans with a tiny state
    machine that tracks string entry via unescaped `"`, so braces inside string
    literals don't throw off the depth count.

    Does NOT fix unescaped quotes inside string values — that's the repair
    path's job. Raises ValueError when no balanced object is present.
    """
    start = text.find("{")
    if start < 0:
        raise ValueError("no '{' in text")
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if escaped:
            escaped = False
            continue
        if in_string:
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("unbalanced '{' without matching '}' in text")


def _strip_code_fences(raw: str) -> str:
    """Strip ```json ... ``` or ``` ... ``` fences if present."""
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3].strip()
    return raw


def _dump_failed_parse(
    raw: str, error: str, cache_path: Path, title: str
) -> Path | None:
    """Write the raw LLM text to a sibling file for postmortem.

    File name: `_failed_{slug}_{YYYYmmdd_HHMMSS}.txt` in the same directory as
    the cache path. Writes `error: ...` header followed by the raw text.
    Also prints a short preview to stderr so the console run has visible
    signal. Returns the dump path, or None on write failure.
    """
    import datetime

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slug_title(title)
    dump_path = cache_path.parent / f"_failed_{slug}_{ts}.txt"
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text(f"error: {error}\n---\n{raw}\n", encoding="utf-8")
    except OSError as exc:
        print(
            f"[producer.script] dump write failed for {dump_path}: {exc!r}",
            file=sys.stderr,
        )
        dump_path = None
    preview = raw[:200].replace("\n", "\\n")
    print(
        f"[producer.script] JSON parse failed for {title!r}: {error} — "
        f"raw preview: {preview!r}"
        + (f" (full dump: {dump_path})" if dump_path else ""),
        file=sys.stderr,
    )
    return dump_path


_JSON_REPAIR_SYSTEM_PROMPT = """\
You fix JSON syntax. The user will give you a string that was meant to be a
single JSON object but has syntax errors. Return ONLY the corrected JSON
object, with no commentary, no markdown fences, no explanation.

Rules:
- Any `"` character inside a string value must be escaped as `\\"`.
- Any newline inside a string value must be escaped as `\\n`.
- Any backslash inside a string value must be doubled as `\\\\`.
- Preserve the original content. Only fix syntax.
"""


async def _repair_json_retry(raw: str, error: str) -> str | None:
    """One JSON-repair call. Returns the repaired text, or None on failure.

    No tools, small prompt, short timeout. Does NOT re-parse here — the caller
    parses and decides what to do if repair still produces invalid JSON
    (usually: fall through to the hook-narration path).
    """
    user_msg = (
        f"Fix the JSON syntax in the following text. "
        f"The parser reported: {error}\n\n"
        f"Original text:\n{raw}"
    )
    try:
        response = await asyncio.to_thread(
            _client.messages.create,
            model=MODEL,
            max_tokens=SEGMENT_MAX_TOKENS,
            system=_JSON_REPAIR_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            timeout=20.0,
        )
        return _strip_code_fences(_extract_segment_text(response))
    except (anthropic.APIError, ValueError) as exc:
        print(
            f"[producer.script] JSON repair call failed: {exc!r}",
            file=sys.stderr,
        )
        return None


_HOOK_FALLBACK_SYSTEM_PROMPT = """\
You narrate a single radio segment as plain spoken prose. Do NOT search the
web. Narrate from the pitch's `hook`, `source_refs`, and `data` fields in a
warm, conversational voice, like a knowledgeable friend.

Follow the pitch's `claim_kind` for temporal framing (same rules as the main
prompt: durable / rising / discovery / neutral).

Do NOT recite listener proper nouns (channel names, video titles, source_refs)
on-air — they are context only. For the alices agent, narrate in third
person ("Alice has been into X"); never address curator taste as "you".

The listener reads and hears English and Chinese fluently, and recognises
some Japanese. Keep any Chinese or Japanese proper noun, title, phrase, or
quote in its original script — no translation, no pinyin / romaji, no
parenthetical glosses.

Target `target_words` at conversational pace. Landing short is fine.

Never emit `<cite>`, `<br>`, or any other inline HTML / XML markup — the
spoken prose is read aloud, and tags come through as garbage audio.

Return ONLY the spoken prose — no JSON, no markdown, no commentary, no stage
directions, no labels. Plain text only.
"""


async def _hook_fallback_narration(
    segment: Pitch, brief: Brief, is_first: bool
) -> SegmentScript:
    """Plain-text narration fallback when JSON parsing fails twice.

    Wraps the model's prose into a SegmentScript in code. `segue_in` is empty
    when `is_first=True` (invariant), else a safe default connector. Estimated
    length is computed from word count at the current WPM.
    """
    wpm = words_per_min()
    target_words = _target_words(segment["suggested_length_sec"], wpm)
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
        "target_words": target_words,
        "words_per_minute": wpm,
    }
    response = await asyncio.to_thread(
        _client.messages.create,
        model=MODEL,
        max_tokens=1024,
        system=_HOOK_FALLBACK_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
        timeout=20.0,
    )
    text = _extract_segment_text(response)
    word_count = max(1, len(text.split()))
    estimated_sec = max(1, round(word_count * 60 / wpm))
    return SegmentScript(
        agent=segment["agent"],
        pitch_title=segment["title"],
        segue_in="" if is_first else "Meanwhile,",
        script=text,
        estimated_length_sec=estimated_sec,
    )


def _validate_segment(seg: SegmentScript, is_first: bool) -> None:
    """Enforce first-segue-empty, script-length floor, and segue-cap warning.

    Same checks the old generate_segment ran inline — lifted out so both the
    cache-hit path and the LLM path enforce them identically.
    """
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


async def generate_segment(
    segment: Pitch,
    brief: Brief,
    is_first: bool,
) -> SegmentScript:
    """Single async LLM call producing one SegmentScript.

    Taste segments (youtube, alices) use the web_search tool server-side;
    weather/calendar segments narrate from `data` only and typically flow
    through the separate opener prompt.

    Same-day cache: on success, writes a {segment, debug} artifact at
    `$RADIO_CACHE_DIR/segment_scripts/{agent}_{slug}_{YYYYMMDD}_{wpm}.json`.
    A matching cache file short-circuits the LLM call entirely.

    Enforces first-segue-empty and script-length-floor on every path
    (cache hit, story outcome, hook-narration fallback).

    Emits on fallback: `producer.segment.research_fallback`.
    Emits on cache: `producer.segment.cache_hit` or `producer.segment.cache_written`.
    No `script.segment.done` emission here — that belongs to `stream_episode_script`.
    """
    if os.environ.get("DISABLE_LLM"):
        raise RuntimeError("LLM disabled via DISABLE_LLM env var")

    wpm = words_per_min()
    date = brief["today_context"]["date"]
    cache_path = _segment_cache_path(segment["agent"], segment["title"], date, wpm)

    cached = _read_cached_segment(cache_path)
    if cached is not None:
        emit(
            "producer.segment.cache_hit",
            {
                "agent": cached["agent"],
                "pitch_title": cached["pitch_title"],
                "cache_path": str(cache_path),
            },
        )
        _validate_segment(cached, is_first)
        return cached

    payload = build_segment_payload(segment, brief, is_first)
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
        tools=[
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 2,
            }
        ],
        timeout=40.0,
    )

    # Observed signals from the primary response — ground truth, not LLM
    # self-report. Kept stable across the parse/repair/hook-narration paths
    # below so the debug artifact reflects what the model actually did on its
    # first swing, even if we later swapped the narration in via fallback.
    search_tool_calls = _count_web_search_uses(response)
    search_queries = _extract_search_queries(response)

    raw = ""
    parse_error: str | None = None
    data: dict | None = None
    try:
        raw = _strip_code_fences(_extract_segment_text(response))
        data = json.loads(_extract_json_object(raw))
    except (ValueError, json.JSONDecodeError) as exc:
        parse_error = f"{type(exc).__name__}: {exc}"

    fallback_path: str | None = None
    seg: SegmentScript | None = None

    if parse_error is None and data is not None:
        try:
            seg = SegmentScript(
                agent=data["agent"],
                pitch_title=data["pitch_title"],
                segue_in=data.get("segue_in", ""),
                script=data["script"],
                estimated_length_sec=data.get("estimated_length_sec", 60),
            )
        except KeyError as exc:
            parse_error = f"missing required key: {exc}"

    if seg is None:
        _dump_failed_parse(raw, parse_error or "unknown", cache_path, segment["title"])

        # Attempt 2: JSON repair. Only try if we have text to repair.
        if raw:
            repaired = await _repair_json_retry(raw, parse_error or "")
            if repaired:
                try:
                    data = json.loads(_extract_json_object(repaired))
                    seg = SegmentScript(
                        agent=data["agent"],
                        pitch_title=data["pitch_title"],
                        segue_in=data.get("segue_in", ""),
                        script=data["script"],
                        estimated_length_sec=data.get("estimated_length_sec", 60),
                    )
                    fallback_path = "repaired"
                    raw = repaired  # surface the repaired text in debug
                except (ValueError, json.JSONDecodeError, KeyError) as exc:
                    print(
                        f"[producer.script] repair attempt still invalid: "
                        f"{type(exc).__name__}: {exc} — escalating to hook-narration",
                        file=sys.stderr,
                    )
                    seg = None

        # Attempt 3: hook-narration fallback. Prose-only LLM call, no tools,
        # no JSON. Builds SegmentScript in code. Keeps the "cannot drop
        # segments" contract intact.
        if seg is None:
            seg = await _hook_fallback_narration(segment, brief, is_first)
            fallback_path = "hook_narration"

        emit(
            "producer.segment.parse_fallback",
            {
                "agent": seg["agent"],
                "pitch_title": seg["pitch_title"],
                "variant": fallback_path,
                "error": parse_error,
            },
        )

    if search_tool_calls == 0:
        emit(
            "producer.segment.research_fallback",
            {
                "agent": seg["agent"],
                "pitch_title": seg["pitch_title"],
                "reason": "no_search",
            },
        )

    seg["script"] = _strip_inline_markup(seg["script"])
    seg["segue_in"] = _strip_inline_markup(seg["segue_in"])

    _validate_segment(seg, is_first)

    debug = {
        "search_tool_calls": search_tool_calls,
        "search_queries": search_queries,
        "fallback_path": fallback_path,
        "raw_llm_text": raw,
        "input_pitch": {
            "title": segment["title"],
            "hook": segment["hook"],
            "source_refs": list(segment.get("source_refs", [])),
            "claim_kind": segment.get("claim_kind", "neutral"),
        },
        "target_words": _target_words(segment["suggested_length_sec"], wpm),
        "words_per_minute": wpm,
    }
    try:
        _write_cached_artifact(cache_path, seg, debug)
        emit(
            "producer.segment.cache_written",
            {
                "agent": seg["agent"],
                "pitch_title": seg["pitch_title"],
                "cache_path": str(cache_path),
            },
        )
    except OSError as exc:
        print(
            f"[producer.script] cache write failed for {cache_path}: {exc!r} — continuing",
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

        emit(
            "script.segment.done",
            {
                "index": i,
                "agent": seg["agent"],
                "pitch_title": seg["pitch_title"],
            },
        )

        # Pacing telemetry — measured vs. target drift. Option-A (prompt-only)
        # pacing enforcement: no retry loop, just instrumentation so a later
        # escalation to retry has real drift numbers behind it.
        wpm = words_per_min()
        target_sec = pitch["suggested_length_sec"]
        spoken_words = len((seg["segue_in"] + " " + seg["script"]).split())
        measured_sec = _words_to_sec(spoken_words, wpm)
        target_words = _target_words(target_sec, wpm)
        emit(
            "producer.segment.pacing_measured",
            {
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
            },
        )

        output_keys.add((seg["agent"], seg["pitch_title"]))
        yield seg

    missing = input_keys - output_keys
    if missing:
        agents = [a for a, _ in missing]
        raise ValueError(f"LLM dropped {len(missing)} segment(s) from agents: {agents}")


_OPENER_DURATION_SEC = 75
_SIGN_OFF_DURATION_SEC = 12


def _render_opener_system_prompt() -> str:
    """Substitute {target_words} with _target_words(_OPENER_DURATION_SEC).

    The template literal keeps time as a named constant (_OPENER_DURATION_SEC,
    in seconds) so code review can see the intended duration; the rendered
    prompt that reaches the LLM contains only the word count.
    """
    return OPENER_SYSTEM_PROMPT.format(
        target_words=_target_words(_OPENER_DURATION_SEC),
    )


def _render_sign_off_system_prompt() -> str:
    """Substitute {target_words} with _target_words(_SIGN_OFF_DURATION_SEC)."""
    return SIGN_OFF_SYSTEM_PROMPT.format(
        target_words=_target_words(_SIGN_OFF_DURATION_SEC),
    )


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


def build_segment_payload(segment: Pitch, brief: Brief, is_first: bool) -> dict:
    """Shape the user-message JSON sent to the segment LLM.

    Single source of truth for the payload — used by `generate_segment` and
    by artifact capture so the on-disk record matches what the LLM saw.
    """
    wpm = words_per_min()
    return {
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
        "target_words": _target_words(segment["suggested_length_sec"], wpm),
        "words_per_minute": wpm,
    }


def build_opener_payload(
    weather_pitch: Pitch | None,
    calendar_pitch: Pitch | None,
    first_content_pitch: Pitch,
    brief: Brief,
) -> dict:
    """Shape the user-message JSON sent to the opener LLM."""
    user_profile = brief.get("user_profile")
    return {
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


def build_sign_off_payload(brief: Brief) -> dict:
    """Shape the user-message JSON sent to the sign-off LLM."""
    return {
        "task": "sign_off",
        "today_context": dict(brief["today_context"]),
        "duration_sec_target": _SIGN_OFF_DURATION_SEC,
        "target_words": _target_words(_SIGN_OFF_DURATION_SEC),
        "words_per_minute": words_per_min(),
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
    payload = build_opener_payload(
        weather_pitch, calendar_pitch, first_content_pitch, brief
    )
    response = await asyncio.to_thread(
        _client.messages.create,
        model=MODEL,
        max_tokens=800,
        system=_render_opener_system_prompt(),
        messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
        timeout=20.0,
    )
    if not response.content or response.content[0].type != "text":
        raise ValueError("LLM returned no text content")
    text = _strip_inline_markup(response.content[0].text.strip())
    if len(text) < _MIN_OPENER_CHARS:
        raise ValueError(
            f"Opener script too short: {len(text)} chars (min {_MIN_OPENER_CHARS})"
        )
    return text


async def generate_sign_off(brief: Brief) -> str:
    """LLM call: ~12s spoken sign-off."""
    if os.environ.get("DISABLE_LLM"):
        raise RuntimeError("LLM disabled via DISABLE_LLM env var")
    payload = build_sign_off_payload(brief)
    response = await asyncio.to_thread(
        _client.messages.create,
        model=MODEL,
        max_tokens=200,
        system=_render_sign_off_system_prompt(),
        messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
        timeout=10.0,
    )
    if not response.content or response.content[0].type != "text":
        raise ValueError("LLM returned no text content")
    return _strip_inline_markup(response.content[0].text.strip())


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

"""Producer LLM pass: per-segment script generation via async iterator (Phase 3 / decision 2a).

Two-step pipeline (prompt_design.md §4):
  Step 1 (deterministic): select_segments() in producer/segments.py
  Step 2 (this module):   LLM writes per-segment scripts via stream_episode_script()

DISABLE_LLM semantics: this module raises RuntimeError on DISABLE_LLM=1 at every
entry point — there is no deterministic script fallback. Research-based
narration for youtube/external segments is LLM-only by construction (the model
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

from agents.external.identity import CURATOR_HANDLE, CURATOR_NAME, LISTENER_HANDLE
from agents.protocol import Brief, Pitch
from producer import DEFAULT_LLM_MODEL, words_per_min
from producer.events import emit

# Prompt text lives in producer/prompts.py. Re-exported here so existing
# callers (and tests) that import from this module continue to work.
from producer.prompts import (
    HOOK_FALLBACK_SYSTEM_PROMPT,
    JSON_REPAIR_SYSTEM_PROMPT,
    OPENER_SYSTEM_PROMPT,
    SIGN_OFF_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
)


# ── Prompt identity rendering ────────────────────────────────────────
#
# SYSTEM_PROMPT contains literal `{` / `}` characters inside its JSON
# examples, so str.format is unsafe. We substitute the three identity
# placeholders by literal string replace. OPENER and SIGN_OFF have no
# literal braces and keep using .format() for {target_words}.

_IDENTITY_SUBSTITUTIONS = {
    "{user_handle}": LISTENER_HANDLE,
    "{curator_handle}": CURATOR_HANDLE,
    "{curator_name}": CURATOR_NAME,
}


def _render_identities(template: str) -> str:
    """Substitute identity placeholders in a prompt template."""
    for placeholder, value in _IDENTITY_SUBSTITUTIONS.items():
        template = template.replace(placeholder, value)
    return template

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

# System prompts moved to producer/prompts.py (imported above).


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
            system=JSON_REPAIR_SYSTEM_PROMPT,
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
        system=_render_identities(HOOK_FALLBACK_SYSTEM_PROMPT),
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
                "text": _render_identities(SYSTEM_PROMPT),
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
        curator_name=CURATOR_NAME,
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

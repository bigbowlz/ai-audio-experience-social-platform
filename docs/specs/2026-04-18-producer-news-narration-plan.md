# Producer news-narration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Producer's YouTube/Alices segment narration from pattern-describing ("you've been into X") into real-world story narration via Anthropic's server-side `web_search` tool, with a same-day on-disk cache and a hook-narration fallback that preserves the `cannot-drop-segments` invariant.

**Architecture:** Single `messages.create` call per segment gains a `web_search_20250305` tool block with `max_uses=2`. The segment LLM derives its own query from the pitch `title` (never from listener proper nouns), broadens once if empty, and falls back to hook-narration if still empty — all in one round trip. Timeout bumps 30s → 40s. A pretty-printed JSON artifact at `$RADIO_CACHE_DIR/segment_scripts/{agent}_{title_slug}_{YYYYMMDD}_{wpm}.json` is written on success (and is the manual-inspection surface for live-LLM tests); cache-hits return verbatim with no LLM call. A new JSON field `research_outcome: "story" | "hook_fallback"` on the LLM response is stripped before yield and drives a `producer.segment.research_fallback` telemetry event.

**Tech Stack:** Python 3.12+, `anthropic` SDK (server-side web_search tool), `pytest` + `pytest-asyncio`, producer's existing in-process event bus.

**Scope boundary (spec §Implementation surface):** local to [producer/script.py](../../producer/script.py) plus one small `cache_dir()` accessor in [producer/__init__.py](../../producer/__init__.py). NO agent-code changes. NO opener / sign-off changes. NO selection-surface changes. Pacing telemetry (`producer.segment.pacing_measured`) is unchanged.

**Hard constraints (from spec):**
- `web_search` tool with `max_uses=2`, segment timeout 40s.
- `cannot-drop-segments` preserved via hook-narration fallback (no segment dropping in v0).
- `DISABLE_LLM=1` continues to raise `RuntimeError`; no offline path added.
- `_MIN_SCRIPT_CHARS = 20` floor still enforced on hook-narration fallback output.
- Listener's channel names / video titles / `source_refs` proper nouns never appear in the story body.
- `claim_kind` directives continue to bound temporal framing in the takeaway.
- `target_words` ceiling semantics from Prompt A unchanged.
- Memory-isolation invariant: segment LLM does not read raw `AgentMemory`, and `ProducerMemory` is not passed to this prompt.

---

## File structure

**Created:**
- `tmp/test_outputs/` — live-LLM test artifact dir (gitignored; created on first live test run via `RADIO_CACHE_DIR=tmp/test_outputs/`).

**Modified:**
- [producer/__init__.py](../../producer/__init__.py) — add `DEFAULT_CACHE_DIR` + `cache_dir()` accessor mirroring `words_per_min()`.
- [producer/script.py](../../producer/script.py) — rewrite `SYSTEM_PROMPT`, update `generate_segment`, add cache internals (`_segment_cache_path`, `_read_cached_segment`, `_write_cached_artifact`, `_slug_title`), update module docstring.
- [tests/test_script.py](../../tests/test_script.py) — add SYSTEM_PROMPT structural assertions for new contract; extend happy-path for `research_outcome` strip.
- [tests/test_script_streaming.py](../../tests/test_script_streaming.py) — add async tests for tool block, timeout, multi-block parse, fallback event, cache read/write.

**New test file:**
- `tests/test_segment_cache.py` — unit tests for `cache_dir()`, `_segment_cache_path`, `_slug_title`, `_read_cached_segment`, `_write_cached_artifact`.
- `tests/test_segment_live.py` — opt-in live-LLM end-to-end test (marked `@pytest.mark.live_llm`; skipped unless `RUN_LIVE_LLM=1`).

**Test-posture decision:** mark live tests with `@pytest.mark.live_llm` and use an autouse skip guard keyed on the env var `RUN_LIVE_LLM=1` (spec §3 Test posture). This keeps the default `pytest` run deterministic and mock-based; live tests are opt-in and write artifacts under `tmp/test_outputs/segment_scripts/` via `RADIO_CACHE_DIR=tmp/test_outputs/` so the user can inspect `{agent}_{title_slug}_{YYYYMMDD}_{wpm}.json`.

---

## Task 1: `cache_dir()` accessor in `producer/__init__.py`

**Files:**
- Modify: [producer/__init__.py](../../producer/__init__.py)
- Test: `tests/test_segment_cache.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_segment_cache.py`:

```python
"""Tests for the producer segment-script cache surface (news-narration spec §3)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from producer import DEFAULT_CACHE_DIR, cache_dir


class TestCacheDirResolver:
    def test_default_is_tmp_segment_script_cache(self):
        assert DEFAULT_CACHE_DIR == Path("tmp/segment_script_cache")

    def test_resolver_returns_default_when_env_absent(self, monkeypatch):
        monkeypatch.delenv("RADIO_CACHE_DIR", raising=False)
        assert cache_dir() == DEFAULT_CACHE_DIR

    def test_resolver_reads_env_override(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))
        assert cache_dir() == tmp_path

    def test_resolver_reads_each_call(self, monkeypatch, tmp_path: Path):
        """No import-time capture — env changes reflect immediately."""
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path / "a"))
        assert cache_dir() == tmp_path / "a"
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path / "b"))
        assert cache_dir() == tmp_path / "b"

    def test_resolver_falls_back_on_empty_string(self, monkeypatch):
        monkeypatch.setenv("RADIO_CACHE_DIR", "")
        assert cache_dir() == DEFAULT_CACHE_DIR
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_segment_cache.py::TestCacheDirResolver -v`
Expected: FAIL with `ImportError: cannot import name 'DEFAULT_CACHE_DIR' from 'producer'`

- [ ] **Step 3: Write minimal implementation**

Edit [producer/__init__.py](../../producer/__init__.py), append after the existing `words_per_min()` function:

```python
from pathlib import Path  # add near the top with the other imports


DEFAULT_CACHE_DIR = Path("tmp/segment_script_cache")
"""Per-segment script artifact directory — one pretty-printed JSON file per
segment LLM call. Same-day hits return `artifact.segment` verbatim without
calling the LLM. Override via RADIO_CACHE_DIR env var (relative or absolute
path). See producer/docs/DESIGN.md and
docs/specs/2026-04-18-producer-news-narration-design.md §3.
"""


def cache_dir() -> Path:
    """Resolve the effective segment-script cache directory.

    Reads RADIO_CACHE_DIR on every call (no import-time capture) so tests can
    point at tmp/test_outputs/ via monkeypatch without reloading the module.
    Falls back to DEFAULT_CACHE_DIR on absent / empty values.
    """
    raw = os.environ.get("RADIO_CACHE_DIR")
    if not raw:
        return DEFAULT_CACHE_DIR
    return Path(raw)
```

Also hoist the `from pathlib import Path` import to the top of the file alongside `import os` (keep alphabetical grouping).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_segment_cache.py::TestCacheDirResolver -v`
Expected: PASS (all 5 tests green).

- [ ] **Step 5: Commit**

```bash
git add producer/__init__.py tests/test_segment_cache.py
git commit -m "feat(producer): add cache_dir() accessor mirroring words_per_min()"
```

---

## Task 2: Cache path helpers — `_slug_title` + `_segment_cache_path`

**Files:**
- Modify: [producer/script.py](../../producer/script.py) (add `_slug_title`, `_segment_cache_path` near the other private helpers).
- Test: `tests/test_segment_cache.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_segment_cache.py`:

```python
from producer.script import _segment_cache_path, _slug_title


class TestSlugTitle:
    def test_lowercases(self):
        assert _slug_title("Jazz Exploration") == "jazz_exploration"

    def test_replaces_non_alphanumerics(self):
        assert _slug_title("Bach's B-minor Mass!") == "bach_s_b_minor_mass"

    def test_collapses_runs(self):
        assert _slug_title("foo   bar   baz") == "foo_bar_baz"
        assert _slug_title("foo-_-bar") == "foo_bar"

    def test_trims_edges(self):
        assert _slug_title("   jazz   ") == "jazz"
        assert _slug_title("!!!jazz!!!") == "jazz"

    def test_handles_unicode_and_digits(self):
        assert _slug_title("Café 2026 — Édition") == "caf_2026_dition"

    def test_empty_or_all_punctuation_returns_underscore(self):
        # Must never produce an empty string (would break the filename).
        assert _slug_title("") == "_"
        assert _slug_title("!!!") == "_"


class TestSegmentCachePath:
    def test_path_shape(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))
        got = _segment_cache_path("youtube", "Jazz Exploration", "2026-04-18", 130)
        assert got == tmp_path / "segment_scripts" / "youtube_jazz_exploration_20260418_130.json"

    def test_strips_date_dashes(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))
        got = _segment_cache_path("alices", "PG Essay", "2026-04-18", 130)
        # YYYYMMDD, not YYYY-MM-DD
        assert "20260418" in got.name
        assert "2026-04-18" not in got.name

    def test_uses_cache_dir_default_when_env_absent(self, monkeypatch):
        monkeypatch.delenv("RADIO_CACHE_DIR", raising=False)
        got = _segment_cache_path("youtube", "x", "2026-04-18", 130)
        assert got == Path("tmp/segment_script_cache/segment_scripts/youtube_x_20260418_130.json")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_segment_cache.py::TestSlugTitle tests/test_segment_cache.py::TestSegmentCachePath -v`
Expected: FAIL with `ImportError: cannot import name '_slug_title'` (or similar).

- [ ] **Step 3: Write minimal implementation**

Edit [producer/script.py](../../producer/script.py), add a `re` import at the top, and add these helpers immediately after the existing `_words_to_sec` helper (and before `generate_segment`):

```python
import re  # add near the other stdlib imports


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
```

(Import `Path` at the top of `producer/script.py` alongside the other imports.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_segment_cache.py -v`
Expected: PASS (all tests in this file green).

- [ ] **Step 5: Commit**

```bash
git add producer/script.py tests/test_segment_cache.py
git commit -m "feat(producer): add title-slug + segment cache path helpers"
```

---

## Task 3: `_read_cached_segment` + `_write_cached_artifact`

**Files:**
- Modify: [producer/script.py](../../producer/script.py)
- Test: `tests/test_segment_cache.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_segment_cache.py`:

```python
from producer.script import (
    SegmentScript,
    _read_cached_segment,
    _write_cached_artifact,
)


def _artifact_dict(**segment_overrides) -> dict:
    seg = {
        "agent": "youtube",
        "pitch_title": "Jazz Exploration",
        "segue_in": "And next —",
        "script": "This is a sufficiently long script body to pass the floor.",
        "estimated_length_sec": 60,
    }
    seg.update(segment_overrides)
    return {
        "segment": seg,
        "debug": {
            "search_query": "jazz",
            "search_used": True,
            "broadened": False,
            "research_outcome": "story",
            "raw_llm_text": "...",
            "input_pitch": {"title": "Jazz Exploration", "hook": "h",
                            "source_refs": [], "claim_kind": "neutral"},
            "target_words": 130,
            "words_per_minute": 130,
        },
    }


class TestWriteCachedArtifact:
    def test_writes_pretty_json(self, tmp_path: Path):
        path = tmp_path / "seg.json"
        art = _artifact_dict()
        _write_cached_artifact(path, art["segment"], art["debug"])
        raw = path.read_text(encoding="utf-8")
        # Pretty-printed → at least one indented line
        assert "\n  " in raw
        loaded = json.loads(raw)
        assert loaded["segment"]["agent"] == "youtube"
        assert loaded["debug"]["search_query"] == "jazz"

    def test_creates_parent_dirs(self, tmp_path: Path):
        path = tmp_path / "does" / "not" / "exist" / "seg.json"
        art = _artifact_dict()
        _write_cached_artifact(path, art["segment"], art["debug"])
        assert path.exists()

    def test_atomic_no_partial_file_on_failure(self, tmp_path: Path, monkeypatch):
        """If os.replace raises, the final path must not exist."""
        path = tmp_path / "seg.json"

        def boom(src, dst):
            raise OSError("simulated rename failure")

        monkeypatch.setattr("os.replace", boom)

        with pytest.raises(OSError):
            art = _artifact_dict()
            _write_cached_artifact(path, art["segment"], art["debug"])
        assert not path.exists()
        # No leftover *.tmp either
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []


class TestReadCachedSegment:
    def test_returns_segment_on_hit(self, tmp_path: Path):
        path = tmp_path / "seg.json"
        art = _artifact_dict()
        path.write_text(json.dumps(art), encoding="utf-8")
        got = _read_cached_segment(path)
        assert got is not None
        assert got["agent"] == "youtube"
        assert got["pitch_title"] == "Jazz Exploration"
        assert got["script"].startswith("This is a sufficiently long")

    def test_returns_none_when_missing(self, tmp_path: Path):
        assert _read_cached_segment(tmp_path / "nope.json") is None

    def test_soft_fails_on_malformed_json(self, tmp_path: Path, capsys):
        path = tmp_path / "seg.json"
        path.write_text("not json at all {{{", encoding="utf-8")
        # Must NOT raise — soft-fail, log, return None (spec §3: cache is advisory).
        assert _read_cached_segment(path) is None

    def test_soft_fails_on_missing_segment_key(self, tmp_path: Path):
        path = tmp_path / "seg.json"
        path.write_text(json.dumps({"debug": {}}), encoding="utf-8")
        assert _read_cached_segment(path) is None

    def test_soft_fails_on_missing_required_segment_field(self, tmp_path: Path):
        art = _artifact_dict()
        del art["segment"]["script"]
        path = tmp_path / "seg.json"
        path.write_text(json.dumps(art), encoding="utf-8")
        assert _read_cached_segment(path) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_segment_cache.py::TestWriteCachedArtifact tests/test_segment_cache.py::TestReadCachedSegment -v`
Expected: FAIL with `ImportError: cannot import name '_read_cached_segment'`.

- [ ] **Step 3: Write minimal implementation**

Edit [producer/script.py](../../producer/script.py). Add these helpers below the cache-path helpers from Task 2:

```python
_SEGMENT_REQUIRED_KEYS = {"agent", "pitch_title", "segue_in", "script", "estimated_length_sec"}


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
        segue_in=segment["segue_in"],
        script=segment["script"],
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
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_segment_cache.py -v`
Expected: PASS (all cache unit tests green).

- [ ] **Step 5: Commit**

```bash
git add producer/script.py tests/test_segment_cache.py
git commit -m "feat(producer): add atomic cache read/write for segment artifacts"
```

---

## Task 4: Rewrite `SYSTEM_PROMPT` for news-narration

**Files:**
- Modify: [producer/script.py](../../producer/script.py) — `SYSTEM_PROMPT` constant.
- Test: [tests/test_script.py](../../tests/test_script.py) — extend `TestSystemPrompt`.

- [ ] **Step 1: Write the failing structural tests**

Add to [tests/test_script.py](../../tests/test_script.py) inside `TestSystemPrompt`:

```python
    def test_has_web_search_usage_block(self):
        """System prompt instructs the model on web_search tool usage + query rules."""
        assert "web_search" in SYSTEM_PROMPT
        # Query-derivation rule: title-only seed, no listener proper nouns.
        assert "title" in SYSTEM_PROMPT
        # Must forbid pulling source_refs (listener proper nouns) into the query.
        assert "source_refs" in SYSTEM_PROMPT
        # Fallback discipline: broaden once, then hook-narration.
        assert "broaden" in SYSTEM_PROMPT.lower()

    def test_has_narration_contract_block(self):
        """Narration contract: segue → story lead → development → takeaway."""
        prompt_lower = SYSTEM_PROMPT.lower()
        for beat in ("story lead", "development", "takeaway"):
            assert beat in prompt_lower, f"missing narration beat: {beat!r}"

    def test_has_source_recitation_rule(self):
        """Listener proper nouns NOT spoken inside the story body."""
        prompt_lower = SYSTEM_PROMPT.lower()
        # Explicit rule forbidding recitation of channel names / video titles / source_refs.
        assert "recit" in prompt_lower  # matches "recite" / "recitation"
        assert "source_refs" in SYSTEM_PROMPT

    def test_has_research_outcome_output_field(self):
        """Output schema carries research_outcome so the fallback path is machine-readable."""
        assert "research_outcome" in SYSTEM_PROMPT
        assert '"story"' in SYSTEM_PROMPT
        assert '"hook_fallback"' in SYSTEM_PROMPT

    def test_forbids_explicit_bridge(self):
        """Prompt forbids 'because you watched X, here's Y' explicit bridges."""
        prompt_lower = SYSTEM_PROMPT.lower()
        # One of these signal phrases must appear in the forbidden-patterns section.
        assert "explicit bridge" in prompt_lower or "because you watched" in prompt_lower
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_script.py::TestSystemPrompt -v`
Expected: FAIL on each new assertion.

- [ ] **Step 3: Write the new SYSTEM_PROMPT**

Replace the entire `SYSTEM_PROMPT = """\...""" ` block in [producer/script.py](../../producer/script.py) with the version below. Preserve `OPENER_SYSTEM_PROMPT` and `SIGN_OFF_SYSTEM_PROMPT` untouched. Also update the module docstring at the top of the file to note research-narration is LLM-only by design (see Step 3b).

```python
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

**Query derivation:**
- Derive your search query from the pitch's `title` field. You may \
  optionally append today's date from `today_context.date` for freshness \
  framing (e.g. `"underwater photography 2026"`).
- NEVER include the listener's channel names, video titles, or any proper \
  nouns from `source_refs` in the search query. `source_refs` are listener \
  data — they stay out of search input.
- Prefer short, topical queries: `"underwater photography"` beats \
  `"National Geographic underwater photography documentary site:nationalgeographic.com"`.

**Primary search + broadened retry:**
1. Issue one query derived from `title`.
2. If the primary search returns nothing topical or nothing fresh within the \
   last ~30 days on the topic, issue ONE broadened retry — drop a "news" \
   qualifier if you added one, or climb to a parent topic \
   (e.g., `"underwater photography news"` → `"photography"`).
3. Do not issue more than 2 searches total.

**Hook-narration fallback:**
If both searches come back empty or nothing is usable as a story, fall back \
to narrating from the pitch `hook` / `source_refs` / `data` in the data-pattern \
voice — the pre-research behavior. The segment still airs (the system cannot \
drop segments in v0). Set `research_outcome` to `"hook_fallback"` in your \
output JSON so the system can log it.

## Narration contract (taste segments)

Internal beats — never announced, never labeled. One continuous passage.

- **Segue in** — `segue_in` field, ≤6 words. Micro-bridge from the previous \
  segment. See Segue style.
- **Story lead** (~20% of `target_words`) — drop straight into the news item: \
  who, where, what. NO "here's a story about X" announcement. NO "this week \
  in photography…" framing.
- **Development** (~55%) — what happened, why it matters, one vivid detail \
  from the search results.
- **Takeaway** (~25%) — land it. An IMPLICIT tie to the listener's domain \
  is permitted (e.g., "the kind of story that travels well in photography \
  circles"). An EXPLICIT BRIDGE is forbidden — never "because you watched X, \
  here's Y", never "since you've been into X…". claim_kind directives still \
  bound temporal framing in the takeaway.

**Source-recitation rule (critical):** the listener's channel names, video \
titles, and any `source_refs` proper nouns are NOT spoken anywhere in the \
story body. The pitch's topic is the shared ground between the listener and \
the story — the listener's data is NOT. You MAY use `source_refs` as context \
to avoid coincidental overlap (e.g., don't pick a story about the exact \
channel the listener already watches), but you MUST NOT recite those names \
on-air.

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
- **alices** — provenance is an EXTERNAL CURATOR (@AlicesLens, \
  pre-captured Day-0 data). Takeaway uses third person — "Alice" or \
  "Alice's lens" — never "you" about curator taste.
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
  `summary`, `start`, `end`, `duration_min`, `attendee_count`, `is_recurring`, \
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
  "estimated_length_sec": 60,
  "research_outcome": "story" or "hook_fallback"
}

- `research_outcome: "story"` when the script body is built from web_search \
  results.
- `research_outcome: "hook_fallback"` when both searches returned nothing \
  usable and the script is narrated from the pitch hook / data / source_refs.

Return ONLY the JSON object — no markdown fences, no commentary.
"""
```

- [ ] **Step 3b: Update the module docstring**

Edit the docstring at the top of [producer/script.py](../../producer/script.py) (lines 1–12) to note research narration:

```python
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
```

- [ ] **Step 4: Run prompt-structure tests to verify they pass**

Run: `pytest tests/test_script.py::TestSystemPrompt -v`
Expected: PASS on every assertion (both pre-existing and new).

Run the full pre-existing file to catch any collateral regression:
`pytest tests/test_script.py tests/test_script_streaming.py tests/test_pacing_and_profile.py -v`
Expected: PASS (all mocks continue to short-circuit the LLM call; prompt changes are tested here only as structural content).

- [ ] **Step 5: Commit**

```bash
git add producer/script.py tests/test_script.py
git commit -m "feat(producer): rewrite SYSTEM_PROMPT for news-narration via web_search"
```

---

## Task 5: Wire the `web_search` tool into `generate_segment` + bump timeout

**Files:**
- Modify: [producer/script.py](../../producer/script.py) — `generate_segment` body.
- Test: [tests/test_script_streaming.py](../../tests/test_script_streaming.py).

The existing `generate_segment` mocks used `monkeypatch.setattr("producer.script.generate_segment", ...)`. The tests for THIS task need to mock at the `_client.messages.create` level (one level lower) so we can assert on the kwargs.

- [ ] **Step 1: Write the failing tests**

Append to [tests/test_script_streaming.py](../../tests/test_script_streaming.py):

```python
from types import SimpleNamespace


def _resp_text(text: str):
    """Mock an Anthropic response whose content is a single text block (no tool use)."""
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


def _segment_json(
    *,
    agent: str = "youtube",
    pitch_title: str = "yt",
    segue_in: str = "",
    script: str = "x" * 50,
    estimated_length_sec: int = 60,
    research_outcome: str = "story",
) -> str:
    import json
    return json.dumps({
        "agent": agent, "pitch_title": pitch_title,
        "segue_in": segue_in, "script": script,
        "estimated_length_sec": estimated_length_sec,
        "research_outcome": research_outcome,
    })


class TestGenerateSegmentToolPlumbing:
    @pytest.mark.asyncio
    async def test_web_search_tool_block_in_create_call(self, monkeypatch, tmp_path):
        """generate_segment passes a web_search_20250305 tool with max_uses=2 and timeout=40s."""
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))
        captured = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            return _resp_text(_segment_json(pitch_title="yt"))

        monkeypatch.setattr("producer.script._client.messages.create", fake_create)

        await generate_segment(_pitch("youtube", "yt"), _brief(), is_first=True)

        assert captured["timeout"] == 40.0
        tools = captured.get("tools")
        assert tools, "generate_segment must pass tools= with the web_search block"
        web = next((t for t in tools if t.get("type", "").startswith("web_search")), None)
        assert web is not None, f"no web_search tool in {tools!r}"
        assert web["max_uses"] == 2

    @pytest.mark.asyncio
    async def test_payload_does_not_leak_source_refs_into_query_seed(
        self, monkeypatch, tmp_path
    ):
        """The user payload carries title + source_refs, but the system prompt owns
        the query-derivation rule. This test pins the payload shape so future
        refactors can't accidentally pre-concatenate source_refs into a `query`
        field that the LLM would then use verbatim."""
        import json as _json
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))
        captured = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            return _resp_text(_segment_json(pitch_title="yt"))

        monkeypatch.setattr("producer.script._client.messages.create", fake_create)

        pitch = _pitch("youtube", "yt")
        pitch["source_refs"] = ["BlueNote", "NationalGeographic"]
        await generate_segment(pitch, _brief(), is_first=True)

        payload = _json.loads(captured["messages"][0]["content"])
        # source_refs stays in the segment block (the LLM needs it for the
        # recitation-avoidance context).
        assert payload["segment"]["source_refs"] == ["BlueNote", "NationalGeographic"]
        # But there is NO top-level "query" or "search_seed" field — the LLM derives its own.
        assert "query" not in payload
        assert "search_seed" not in payload
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_script_streaming.py::TestGenerateSegmentToolPlumbing -v`
Expected: FAIL (no `tools` kwarg yet; timeout still 30.0).

- [ ] **Step 3: Modify `generate_segment`**

In [producer/script.py](../../producer/script.py), update the `_client.messages.create` call inside `generate_segment`:

```python
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
```

(Keep the rest of `generate_segment` — response parsing, validation — unchanged for this task. Multi-block parsing is Task 6.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_script_streaming.py -v`
Expected: PASS, including the two new tests. Existing tests that mock `producer.script.generate_segment` (not the deeper `_client.messages.create`) still pass because they bypass this code path entirely.

- [ ] **Step 5: Commit**

```bash
git add producer/script.py tests/test_script_streaming.py
git commit -m "feat(producer): add web_search tool to segment call, bump timeout 30→40s"
```

---

## Task 6: Parse multi-block responses + extract final JSON

**Files:**
- Modify: [producer/script.py](../../producer/script.py) — introduce `_extract_segment_json` helper; call it from `generate_segment`.
- Test: [tests/test_script_streaming.py](../../tests/test_script_streaming.py).

When the model uses `web_search`, `response.content` may contain `server_tool_use`, `web_search_tool_result`, and finally one or more `text` blocks. The existing code reads `response.content[0].text` — this breaks the moment a tool block appears first.

- [ ] **Step 1: Write the failing tests**

Append to [tests/test_script_streaming.py](../../tests/test_script_streaming.py):

```python
def _resp_with_tool_blocks(final_text: str):
    """Mock a response whose content list has tool blocks BEFORE the final text."""
    return SimpleNamespace(content=[
        SimpleNamespace(type="server_tool_use", name="web_search", input={"query": "jazz"}),
        SimpleNamespace(type="web_search_tool_result", content=[]),
        SimpleNamespace(type="text", text=final_text),
    ])


def _resp_text_then_more_text(first: str, second: str):
    """Mock a response with two text blocks. We take the last."""
    return SimpleNamespace(content=[
        SimpleNamespace(type="text", text=first),
        SimpleNamespace(type="text", text=second),
    ])


class TestGenerateSegmentMultiBlockParse:
    @pytest.mark.asyncio
    async def test_extracts_final_text_block_after_tool_use(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))

        def fake_create(**kwargs):
            return _resp_with_tool_blocks(_segment_json(pitch_title="yt", script="x" * 50))

        monkeypatch.setattr("producer.script._client.messages.create", fake_create)

        seg = await generate_segment(_pitch("youtube", "yt"), _brief(), is_first=True)
        assert seg["pitch_title"] == "yt"
        assert len(seg["script"]) >= 20

    @pytest.mark.asyncio
    async def test_uses_last_text_block_when_multiple(self, monkeypatch, tmp_path):
        """If the model emits intermediate commentary text then final JSON, take the last."""
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))

        def fake_create(**kwargs):
            return _resp_text_then_more_text(
                "Let me search for that.",
                _segment_json(pitch_title="yt", script="x" * 50),
            )

        monkeypatch.setattr("producer.script._client.messages.create", fake_create)

        seg = await generate_segment(_pitch("youtube", "yt"), _brief(), is_first=True)
        assert seg["pitch_title"] == "yt"

    @pytest.mark.asyncio
    async def test_raises_when_no_text_block(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))

        def fake_create(**kwargs):
            return SimpleNamespace(content=[
                SimpleNamespace(type="server_tool_use", name="web_search", input={}),
            ])

        monkeypatch.setattr("producer.script._client.messages.create", fake_create)

        with pytest.raises(ValueError, match="no text content"):
            await generate_segment(_pitch("youtube", "yt"), _brief(), is_first=True)

    @pytest.mark.asyncio
    async def test_strips_research_outcome_from_yielded_segment(
        self, monkeypatch, tmp_path
    ):
        """research_outcome is telemetry only — never appears on the returned SegmentScript."""
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))

        def fake_create(**kwargs):
            return _resp_text(_segment_json(
                pitch_title="yt", script="x" * 50, research_outcome="story",
            ))

        monkeypatch.setattr("producer.script._client.messages.create", fake_create)

        seg = await generate_segment(_pitch("youtube", "yt"), _brief(), is_first=True)
        assert "research_outcome" not in seg
        assert set(seg.keys()) == {"agent", "pitch_title", "segue_in", "script", "estimated_length_sec"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_script_streaming.py::TestGenerateSegmentMultiBlockParse -v`
Expected: the first test fails with `AttributeError: 'SimpleNamespace' object has no attribute 'text'` (because `content[0]` is a `server_tool_use`). The `research_outcome` test fails because the current parser copies the key through.

- [ ] **Step 3: Implement `_extract_segment_json`**

Add this helper in [producer/script.py](../../producer/script.py) above `generate_segment`:

```python
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
```

Update `generate_segment` to use it and to strip `research_outcome` from the SegmentScript. Replace this block:

```python
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
```

with:

```python
    raw = _extract_segment_text(response)
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3].strip()

    data = json.loads(raw)
    research_outcome = data.get("research_outcome", "story")

    seg = SegmentScript(
        agent=data["agent"],
        pitch_title=data["pitch_title"],
        segue_in=data.get("segue_in", ""),
        script=data["script"],
        estimated_length_sec=data.get("estimated_length_sec", 60),
    )
```

(The `research_outcome` variable is captured here for use in Task 7's fallback-telemetry emission; it is NOT written onto `seg`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_script_streaming.py -v`
Expected: PASS on all tests including the four new multi-block tests.

- [ ] **Step 5: Commit**

```bash
git add producer/script.py tests/test_script_streaming.py
git commit -m "feat(producer): parse multi-block LLM response; strip research_outcome from output"
```

---

## Task 7: Emit `producer.segment.research_fallback` telemetry on hook-narration

**Files:**
- Modify: [producer/script.py](../../producer/script.py) — `generate_segment` emits the event when `research_outcome == "hook_fallback"`.
- Test: [tests/test_script_streaming.py](../../tests/test_script_streaming.py).

- [ ] **Step 1: Write the failing tests**

Append to [tests/test_script_streaming.py](../../tests/test_script_streaming.py):

```python
from producer.events import EventBus, set_default_bus


class TestResearchFallbackTelemetry:
    @pytest.mark.asyncio
    async def test_hook_fallback_emits_event(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))
        bus = EventBus()
        captured: list[tuple[str, dict]] = []
        bus.subscribe(lambda n, p: captured.append((n, p)))
        set_default_bus(bus)
        try:
            def fake_create(**kwargs):
                return _resp_text(_segment_json(
                    pitch_title="yt", script="x" * 50,
                    research_outcome="hook_fallback",
                ))

            monkeypatch.setattr("producer.script._client.messages.create", fake_create)
            await generate_segment(_pitch("youtube", "yt"), _brief(), is_first=True)
        finally:
            set_default_bus(EventBus())

        events = [(n, p) for n, p in captured
                  if n == "producer.segment.research_fallback"]
        assert len(events) == 1
        name, payload = events[0]
        assert payload["agent"] == "youtube"
        assert payload["pitch_title"] == "yt"
        assert "reason" in payload  # "empty_search" | "broadened_empty"

    @pytest.mark.asyncio
    async def test_story_outcome_does_not_emit_event(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))
        bus = EventBus()
        captured: list[tuple[str, dict]] = []
        bus.subscribe(lambda n, p: captured.append((n, p)))
        set_default_bus(bus)
        try:
            def fake_create(**kwargs):
                return _resp_text(_segment_json(
                    pitch_title="yt", script="x" * 50, research_outcome="story",
                ))
            monkeypatch.setattr("producer.script._client.messages.create", fake_create)
            await generate_segment(_pitch("youtube", "yt"), _brief(), is_first=True)
        finally:
            set_default_bus(EventBus())

        events = [n for n, _ in captured if n == "producer.segment.research_fallback"]
        assert events == []

    @pytest.mark.asyncio
    async def test_hook_fallback_still_enforces_min_script_floor(
        self, monkeypatch, tmp_path
    ):
        """Spec invariant: _MIN_SCRIPT_CHARS (20) still applies to hook-fallback output."""
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))

        def fake_create(**kwargs):
            return _resp_text(_segment_json(
                pitch_title="yt",
                script="too short.",  # 10 chars
                research_outcome="hook_fallback",
            ))

        monkeypatch.setattr("producer.script._client.messages.create", fake_create)

        with pytest.raises(ValueError, match="too short"):
            await generate_segment(_pitch("youtube", "yt"), _brief(), is_first=True)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_script_streaming.py::TestResearchFallbackTelemetry -v`
Expected: the `hook_fallback_emits_event` test FAILS (no emission yet). The `min_script_floor` test passes already because the existing floor check catches short scripts — run both anyway to confirm the spec invariant.

- [ ] **Step 3: Emit the event in `generate_segment`**

In [producer/script.py](../../producer/script.py), after building `seg` and before the validation block that currently exists (the `is_first` / `_MIN_SCRIPT_CHARS` checks), add:

```python
    if research_outcome == "hook_fallback":
        emit(
            "producer.segment.research_fallback",
            {
                "agent": seg["agent"],
                "pitch_title": seg["pitch_title"],
                "reason": "empty_search",  # v0: the LLM does not distinguish
                                           # empty_search vs broadened_empty in its
                                           # single-field research_outcome. A future
                                           # enrichment can split this on the LLM side.
            },
        )
```

(Do NOT add an `index` here — `generate_segment` does not know its index. The event is per-segment-call; `stream_episode_script` logs `script.segment.done` with index for correlation.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_script_streaming.py -v`
Expected: PASS on all tests including the three new telemetry tests. The `min_script_floor` test confirms the floor is enforced on the hook-fallback path.

- [ ] **Step 5: Commit**

```bash
git add producer/script.py tests/test_script_streaming.py
git commit -m "feat(producer): emit producer.segment.research_fallback on hook_fallback outcome"
```

---

## Task 8: Same-day cache read + write in `generate_segment`

**Files:**
- Modify: [producer/script.py](../../producer/script.py) — wrap the LLM call with cache read/write; emit cache telemetry.
- Test: [tests/test_script_streaming.py](../../tests/test_script_streaming.py).

- [ ] **Step 1: Write the failing tests**

Append to [tests/test_script_streaming.py](../../tests/test_script_streaming.py):

```python
class TestSegmentCacheIntegration:
    @pytest.mark.asyncio
    async def test_cache_miss_writes_artifact(self, monkeypatch, tmp_path):
        """First call with no cache writes the artifact and emits cache_written."""
        import json as _json
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))

        bus = EventBus()
        captured: list[tuple[str, dict]] = []
        bus.subscribe(lambda n, p: captured.append((n, p)))
        set_default_bus(bus)

        try:
            def fake_create(**kwargs):
                return _resp_text(_segment_json(pitch_title="Jazz", script="x" * 50))

            monkeypatch.setattr("producer.script._client.messages.create", fake_create)

            pitch = _pitch("youtube", "Jazz")
            brief = _brief()
            await generate_segment(pitch, brief, is_first=True)
        finally:
            set_default_bus(EventBus())

        # Artifact present on disk
        date = brief["today_context"]["date"].replace("-", "")
        expected = tmp_path / "segment_scripts" / f"youtube_jazz_{date}_130.json"
        assert expected.exists()
        art = _json.loads(expected.read_text(encoding="utf-8"))
        assert set(art.keys()) == {"segment", "debug"}
        assert art["segment"]["pitch_title"] == "Jazz"
        assert "research_outcome" in art["debug"]
        assert "raw_llm_text" in art["debug"]
        assert "input_pitch" in art["debug"]
        assert art["debug"]["target_words"] == _target_words_helper(pitch["suggested_length_sec"])
        assert art["debug"]["words_per_minute"] == 130

        # cache_written event emitted
        cw = [(n, p) for n, p in captured if n == "producer.segment.cache_written"]
        assert len(cw) == 1
        assert cw[0][1]["agent"] == "youtube"
        assert cw[0][1]["pitch_title"] == "Jazz"
        assert cw[0][1]["cache_path"] == str(expected)

    @pytest.mark.asyncio
    async def test_cache_hit_skips_llm_and_emits_cache_hit(self, monkeypatch, tmp_path):
        """Second call with a matching cache hits and never calls the LLM."""
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))

        def fake_create(**kwargs):
            return _resp_text(_segment_json(pitch_title="Jazz", script="x" * 50))

        monkeypatch.setattr("producer.script._client.messages.create", fake_create)
        pitch = _pitch("youtube", "Jazz")
        brief = _brief()
        # Prime the cache
        await generate_segment(pitch, brief, is_first=True)

        # Now switch the mock to raise — a real call would fail.
        def raising_create(**kwargs):
            raise AssertionError("LLM must not be called on cache hit")

        monkeypatch.setattr("producer.script._client.messages.create", raising_create)

        bus = EventBus()
        captured: list[tuple[str, dict]] = []
        bus.subscribe(lambda n, p: captured.append((n, p)))
        set_default_bus(bus)

        try:
            seg = await generate_segment(pitch, brief, is_first=True)
        finally:
            set_default_bus(EventBus())

        assert seg["pitch_title"] == "Jazz"
        hits = [(n, p) for n, p in captured if n == "producer.segment.cache_hit"]
        assert len(hits) == 1
        assert hits[0][1]["agent"] == "youtube"
        assert hits[0][1]["pitch_title"] == "Jazz"

    @pytest.mark.asyncio
    async def test_cache_hit_different_wpm_is_a_miss(self, monkeypatch, tmp_path):
        """wpm is part of the cache key — changing it invalidates."""
        import json as _json
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))
        monkeypatch.delenv("PRODUCER_WORDS_PER_MIN", raising=False)

        call_count = [0]

        def fake_create(**kwargs):
            call_count[0] += 1
            return _resp_text(_segment_json(pitch_title="Jazz", script="x" * 50))

        monkeypatch.setattr("producer.script._client.messages.create", fake_create)
        pitch = _pitch("youtube", "Jazz")
        brief = _brief()
        await generate_segment(pitch, brief, is_first=True)   # writes wpm=130 file
        assert call_count[0] == 1

        monkeypatch.setenv("PRODUCER_WORDS_PER_MIN", "150")
        await generate_segment(pitch, brief, is_first=True)   # must miss — different wpm
        assert call_count[0] == 2

    @pytest.mark.asyncio
    async def test_cache_write_survives_oserror_on_write(
        self, monkeypatch, tmp_path, capsys
    ):
        """Cache-write failure must not block generation; logs and continues."""
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))

        def fake_create(**kwargs):
            return _resp_text(_segment_json(pitch_title="Jazz", script="x" * 50))

        monkeypatch.setattr("producer.script._client.messages.create", fake_create)

        def bad_write(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr("producer.script._write_cached_artifact", bad_write)

        # Must not raise.
        seg = await generate_segment(_pitch("youtube", "Jazz"), _brief(), is_first=True)
        assert seg["pitch_title"] == "Jazz"


# Small helper for the target_words assertion in the cache artifact test above.
def _target_words_helper(sec: int) -> int:
    from producer.script import _target_words
    return _target_words(sec)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_script_streaming.py::TestSegmentCacheIntegration -v`
Expected: FAIL — no cache logic yet, the artifact file never appears, the LLM is re-called on the second invocation, etc.

- [ ] **Step 3: Implement cache read/write in `generate_segment`**

Update `generate_segment` in [producer/script.py](../../producer/script.py). The full body becomes:

```python
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
        "target_words": _target_words(segment["suggested_length_sec"], wpm),
        "words_per_minute": wpm,
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
        tools=[
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 2,
            }
        ],
        timeout=40.0,
    )

    raw = _extract_segment_text(response)
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3].strip()

    data = json.loads(raw)
    research_outcome = data.get("research_outcome", "story")

    seg = SegmentScript(
        agent=data["agent"],
        pitch_title=data["pitch_title"],
        segue_in=data.get("segue_in", ""),
        script=data["script"],
        estimated_length_sec=data.get("estimated_length_sec", 60),
    )

    if research_outcome == "hook_fallback":
        emit(
            "producer.segment.research_fallback",
            {
                "agent": seg["agent"],
                "pitch_title": seg["pitch_title"],
                "reason": "empty_search",
            },
        )

    _validate_segment(seg, is_first)

    debug = {
        "search_query": None,          # not introspectable from the SDK response shape v0
        "search_used": research_outcome == "story",
        "broadened": False,             # reserved — LLM doesn't report this in v0
        "research_outcome": research_outcome,
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
```

Extract the post-build validation that the old body performed into a helper near the top of the module so both the cache-hit and LLM paths go through it:

```python
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
```

Delete the duplicate inline validation inside `generate_segment` now that `_validate_segment` owns it (the LLM path calls `_validate_segment(seg, is_first)` after the research_outcome event).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_script_streaming.py tests/test_script.py tests/test_segment_cache.py tests/test_pacing_and_profile.py -v`
Expected: PASS on all tests — new cache integration tests, all pre-existing tests, and the pacing suite.

- [ ] **Step 5: Commit**

```bash
git add producer/script.py tests/test_script_streaming.py
git commit -m "feat(producer): same-day segment-script cache + cache_hit/cache_written telemetry"
```

---

## Task 9: Live-LLM opt-in test

**Files:**
- Create: `tests/test_segment_live.py`
- Modify: `pyproject.toml` — register the `live_llm` marker so `pytest --strict-markers` stays clean (the project does not currently use `--strict-markers` but registering is cheap insurance).

- [ ] **Step 1: Register the marker**

Edit [pyproject.toml](../../pyproject.toml):

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = [
    "live_llm: end-to-end test that makes real Anthropic + web_search calls. Skipped unless RUN_LIVE_LLM=1. Writes artifacts under $RADIO_CACHE_DIR.",
]
```

- [ ] **Step 2: Write the live test**

Create `tests/test_segment_live.py`:

```python
"""Opt-in end-to-end test: one real generate_segment() call against a real pitch.

Skipped unless RUN_LIVE_LLM=1. Writes its artifact under
tmp/test_outputs/segment_scripts/ via RADIO_CACHE_DIR=tmp/test_outputs/ so the
user can open the file after the run and audit the segue_in, script body,
research_outcome, and raw_llm_text the model produced.

Spec: docs/specs/2026-04-18-producer-news-narration-design.md §3 Test posture.

Run it:
    RUN_LIVE_LLM=1 pytest tests/test_segment_live.py -v -s

Artifact location after the run:
    tmp/test_outputs/segment_scripts/youtube_underwater_photography_YYYYMMDD_130.json
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agents.protocol import Brief, Pitch
from producer.script import _segment_cache_path, generate_segment


_RUN_LIVE = os.environ.get("RUN_LIVE_LLM") == "1"


@pytest.fixture(autouse=True)
def _guard_live(monkeypatch):
    """Force RADIO_CACHE_DIR=tmp/test_outputs/ for every test in this module.

    The directory is intentionally outside the default cache so real episode
    cache files don't get clobbered by test runs.
    """
    repo_root = Path(__file__).resolve().parents[1]
    out_dir = repo_root / "tmp" / "test_outputs"
    monkeypatch.setenv("RADIO_CACHE_DIR", str(out_dir))
    yield


def _live_pitch() -> Pitch:
    return {
        "agent": "youtube",
        "title": "Underwater photography",
        "hook": (
            "You've been getting into underwater photography lately — "
            "National Geographic's been showing up in your subs more and more."
        ),
        "source_refs": ["National Geographic", "BBC Earth"],
        "data": {},
        "priority": 0.9,
        "thin_signal": False,
        "claim_kind": "rising",
        "provenance_shape": "balanced",
        "suggested_length_sec": 90,
    }


def _brief() -> Brief:
    return {"today_context": {
        "date": "2026-04-18",
        "day_of_week": "Saturday",
        "time_of_day": "morning",
        "weather_summary": None,
        "calendar_events": None,
    }}


@pytest.mark.live_llm
@pytest.mark.asyncio
@pytest.mark.skipif(not _RUN_LIVE, reason="Set RUN_LIVE_LLM=1 to run live LLM tests")
async def test_generate_segment_writes_inspectable_artifact(tmp_path):
    """Live end-to-end: real LLM + real web_search; writes an inspectable artifact.

    After the test passes, open the file at the asserted path and audit:
    - `segment.segue_in` is empty (is_first=True) or ≤6 words.
    - `segment.script` reads like a news story, not a listener-data restatement.
    - `debug.research_outcome` is "story" (broadened search fell back to "hook_fallback").
    - `debug.raw_llm_text` contains the model's raw JSON output.
    - Listener proper nouns ("National Geographic", "BBC Earth") do NOT appear
      in `segment.script` — the source-recitation rule must hold.
    """
    pitch = _live_pitch()
    brief = _brief()

    seg = await generate_segment(pitch, brief, is_first=True)

    assert seg["agent"] == "youtube"
    assert seg["pitch_title"] == "Underwater photography"
    assert seg["segue_in"] == ""
    assert len(seg["script"]) >= 100   # real narration, not a stub
    # Source-recitation invariant — listener proper nouns forbidden in the body.
    assert "National Geographic" not in seg["script"]
    assert "BBC Earth" not in seg["script"]

    # Artifact must exist and be inspectable.
    wpm = 130
    expected = _segment_cache_path(
        pitch["agent"], pitch["title"], brief["today_context"]["date"], wpm
    )
    assert expected.exists(), f"artifact missing at {expected}"
    art = json.loads(expected.read_text(encoding="utf-8"))
    assert art["segment"]["pitch_title"] == "Underwater photography"
    assert art["debug"]["research_outcome"] in ("story", "hook_fallback")
    assert "raw_llm_text" in art["debug"]
    assert art["debug"]["input_pitch"]["title"] == "Underwater photography"
    print(f"\nLIVE ARTIFACT: {expected}")
```

- [ ] **Step 3: Sanity-run in mock mode**

Run: `pytest tests/test_segment_live.py -v`
Expected: SKIPPED (reason: `Set RUN_LIVE_LLM=1 to run live LLM tests`).

- [ ] **Step 4: Skip the live run at this step**

Do not run with `RUN_LIVE_LLM=1` here — that belongs to the verification phase (Task 10). Just confirm the test is wired correctly.

- [ ] **Step 5: Commit**

```bash
git add tests/test_segment_live.py pyproject.toml
git commit -m "test(producer): opt-in live-LLM test for news-narration + artifact inspection"
```

---

## Task 10: Verification — full suite + live artifact audit

This task corresponds to the `superpowers:verification-before-completion` gate the user asked for. It is a verification step, not a code change.

- [ ] **Step 1: Run the full test suite (mocked)**

Run: `pytest -v`
Expected: every pre-existing test passes AND every test added in Tasks 1–9 passes. Live tests report SKIPPED.

- [ ] **Step 2: Confirm pacing telemetry is unchanged**

Specifically run:
`pytest tests/test_pacing_and_profile.py -v`
Expected: all assertions against `producer.segment.pacing_measured` pass. The news-narration path does not perturb the pacing event — `stream_episode_script` emits it post-generation as before; all that changed is what `generate_segment` returns.

- [ ] **Step 3: Run ONE live-LLM test end-to-end**

Run:
```bash
RUN_LIVE_LLM=1 pytest tests/test_segment_live.py -v -s
```
Expected: PASS. Print output contains the artifact path.

- [ ] **Step 4: Manually audit the artifact**

Open the artifact file printed in Step 3, e.g.:
```
tmp/test_outputs/segment_scripts/youtube_underwater_photography_20260418_130.json
```
Confirm:
- `segment.script` reads like a real news story (not a listener-data restatement).
- No `National Geographic` or `BBC Earth` strings inside `segment.script`.
- `debug.research_outcome == "story"` (or `"hook_fallback"` with a credible reason).
- `debug.raw_llm_text` is present and matches the JSON that produced `segment`.

Report the outcome to the user before closing the task.

- [ ] **Step 5: Final commit (if any doc nits surfaced)**

Only commit if Step 4 surfaced a real prompt adjustment. If the artifact looks clean, no extra commit is needed and the plan is done.

---

## Self-review checklist

**Spec coverage:**
- §1 Research mechanism — Task 5 (web_search tool block, max_uses=2).
- §2 Query derivation — Task 4 (SYSTEM_PROMPT rules) + Task 5's payload-shape test pinning that no server-side query pre-concatenation exists.
- §3 Cost & latency control — Task 5 (timeout), Task 8 (cache read/write), Task 9 (test posture writing under RADIO_CACHE_DIR).
- §3 Artifact shape — Task 8's `debug` dict matches the spec's schema (search_query, search_used, broadened, research_outcome, raw_llm_text, input_pitch, target_words, words_per_minute).
- §3 title_slug normalization — Task 2 tests.
- §3 Corrupted cache soft-fail — Task 3 tests.
- §4 DISABLE_LLM unchanged — preserved in Task 8 rewrite; pre-existing test `test_generate_segment_raises_when_disable_llm_set` still runs.
- §4 Module docstring update — Task 4 Step 3b.
- §5 Broaden-then-fallback — SYSTEM_PROMPT (Task 4); research_outcome field (Task 4 + Task 7 event emission); `_MIN_SCRIPT_CHARS` floor preserved via `_validate_segment` (Task 8).
- §5 Research_fallback telemetry — Task 7.
- §6 Narration contract — SYSTEM_PROMPT (Task 4).
- §6 Source-recitation rule — SYSTEM_PROMPT (Task 4) + live-test assertion (Task 9).
- §6 Per-agent provenance preserved — SYSTEM_PROMPT (Task 4) keeps the youtube/alices voice rules.
- Invariants — cannot-drop-segments, first-segue-empty, _MIN_SCRIPT_CHARS floor, claim_kind directives, target_words ceiling, memory-isolation → all covered by `_validate_segment` plus pre-existing `stream_episode_script` checks plus SYSTEM_PROMPT rules.

**Placeholder scan:** no "TBD", "implement later", "similar to Task N" — every task shows the full code it introduces. No references to types/functions not defined in this plan.

**Type consistency:** `_segment_cache_path`, `_slug_title`, `_read_cached_segment`, `_write_cached_artifact`, `_validate_segment`, `_extract_segment_text` — same spelling everywhere in the plan, same signatures used in tests. `SegmentScript` TypedDict unchanged (no new required keys). `producer.segment.research_fallback`, `producer.segment.cache_hit`, `producer.segment.cache_written` — event names match the spec verbatim.

## Execution Handoff

Two execution options:

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task with two-stage review between tasks.

**2. Inline Execution** — execute tasks in this session via `superpowers:executing-plans` with a batch-commit checkpoint after Task 4 and Task 8.

Awaiting user approval before starting implementation.

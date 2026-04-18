# Producer news-narration — design

**Status:** DRAFT (spec)
**Scope:** Producer script generation only — YouTube and Alices segments.
**Out of scope:** Weather/calendar (live in the unified opener, Prompt B). Cold-open and sign-off (separate prompts, unaffected).
**References:** [`producer/script.py`](../../producer/script.py), [`producer/docs/DESIGN.md`](../../producer/docs/DESIGN.md), [`agents/youtube/agent.py`](../../agents/youtube/agent.py), [`agents/alices/agent.py`](../../agents/alices/agent.py).

## Problem

Today, Producer narrates YouTube/Alices segments by describing patterns in the pitch data ("you've been into underwater photography, National Geographic has been showing up in your subs"). The listener's own data gets read back to them. The pivot: narration should be a **real-world story or news item found via web research**, framed around the pitch's topic — with the pitch's source content serving as an implicit topical anchor, not an explicit bridge.

The connection to the source content may be implicit (the narration can touch the topic area) but must NOT be built explicitly ("because you watched X, here's Y").

## Design decisions

### 1. Research mechanism — Anthropic `web_search` tool

Use Anthropic's server-side `web_search` tool inside the same `messages.create` call that generates the segment script. One round-trip: the segment LLM calls `web_search` with its own query, receives results, and writes the narration in one pass.

Rationale: lowest infra overhead (no new secret, no new HTTP dependency, no new fallback surface); stays on the existing segment LLM call path; `max_uses` bounds cost deterministically. Cost is ~$0.02–$0.04/episode at 2 content segments × 1–2 searches each (broadening is best-case only).

### 2. Query derivation — in-call, from `title` only

The segment LLM derives its own `web_search` query from the pitch's `title` field, optionally combined with `today_context.date` for freshness framing. The system prompt forbids including the listener's channel names, video titles, or proper nouns from `source_refs` in the search query.

Rationale: `title` is the topic label — the clean search seed. Mixing in `source_refs[0]` (a channel name) skews results toward creator-specific gossip instead of topical news. `source_refs` stays in the prompt as context the LLM uses to avoid recitation, not as search material. If `title` alone is too generic in practice, a follow-up adds `search_seed: str` to the `Pitch` protocol.

### 3. Cost & latency control — bounded search + same-day cache

**Bounded search:** `web_search` configured with `max_uses=2` per segment call — one for the primary query, one for the broadened retry (see §5). The LLM handles both attempts inside a single `messages.create` call; no client-side retry loop. Segment-call timeout bumps from `30s` → `40s` to absorb up to two search round-trips (~1-3s each).

**Same-day cache:** one pretty-printed JSON artifact per segment at `$RADIO_CACHE_DIR/segment_scripts/{agent}_{title_slug}_{YYYYMMDD}_{wpm}.json` (defaulting to `tmp/segment_script_cache/` with `RADIO_CACHE_DIR` env override). Written at the end of `generate_segment` on success. The artifact is the manual-inspection surface too — one file per LLM call that touched real network, human-readable by design.

**Artifact shape:**

```json
{
  "segment": { "agent": "...", "pitch_title": "...", "segue_in": "...", "script": "...", "estimated_length_sec": 60 },
  "debug": {
    "search_query": "underwater photography",
    "search_used": true,
    "broadened": false,
    "research_outcome": "story",
    "raw_llm_text": "...",
    "input_pitch": { "title": "...", "hook": "...", "source_refs": [...], "claim_kind": "..." },
    "target_words": 130,
    "words_per_minute": 130
  }
}
```

On cache hit, `generate_segment` returns `artifact.segment` verbatim — skips `web_search` and the LLM call entirely. `artifact.debug` is written for inspection but ignored on read. TTL is implicit in the date key (next calendar day = cache miss).

No per-episode search budget counter — 2 content segments × 1 search each is small enough that tighter accounting isn't worth it.

`title_slug` is the pitch title normalized: lowercased, non-alphanumerics → `_`, collapsed runs, trimmed. Cache is advisory — a corrupted or malformed cache file is logged and treated as a miss (not a hard failure).

**Test posture.** Tests that exercise real LLM calls (marked `@pytest.mark.live_llm` or equivalent) set `RADIO_CACHE_DIR=tmp/test_outputs/` so their artifacts are written to a distinct directory separate from real-episode cache. These artifacts are the primary manual-review surface — after a live-LLM test run, the user opens `tmp/test_outputs/segment_scripts/*.json` to audit the segue, script body, search query, and raw LLM text. Tests that mock the LLM skip cache writes entirely (the mock returns a canned SegmentScript; no artifact is generated).

### 4. `DISABLE_LLM` degradation — unchanged (still raises)

Research-based narration is LLM-only by construction. `generate_segment` continues to raise `RuntimeError` on `DISABLE_LLM=1`. The orchestrator's existing `args.no_llm` gate handles upstream routing; no new offline surface is added.

Update the `producer/script.py` module docstring to note that research narration is intentionally LLM-only.

### 5. Fallback on empty research results — broaden once, then hook-narration

Three-step fallback inside `generate_segment`:

1. **Primary search:** LLM issues one query derived from `title`.
2. **Broadened retry (if primary returns nothing topical):** LLM issues a second `web_search` call with a broader query — drops the `news` qualifier or climbs to a parent topic (e.g., `"underwater photography news"` → `"photography"`). Both attempts happen inside the same `messages.create` call via `max_uses=2`; the LLM decides when to broaden based on explicit prompt criteria (no fresh result within ~30 days on the topic).
3. **Hook-narration fallback (if broadened still empty):** segment LLM writes the narration from the pitch hook / `source_refs` / `data` as in the current data-pattern voice. The segment still airs — preserves the `cannot-drop-segments` invariant in `stream_episode_script`.

Emit `producer.segment.research_fallback` telemetry on every hook-narration fallback with `{agent, pitch_title, reason: "empty_search" | "broadened_empty"}`. This tells us real-world research hit rate before v1.

**v1 follow-up (out of scope for this spec, pencilled under Open questions):** once agents emit queued backup pitches (3-5 per agent, ordered), the script loop will retry the next pitch on research failure and drop the slot only when the queue is exhausted. The `cannot-drop-segments` invariant is updated then, alongside the queue mechanism. Without the queue, drop-on-fail is unsafe in v0 (a single bad search day collapses the episode to ~85s of opener + sign-off).

### 6. Narration contract — segue → story lead → development → takeaway

Internal beats (never announced). Applies to YouTube and Alices segments only.

| Beat | ~% of `target_words` | Constraint |
|---|---|---|
| `segue_in` | (not counted) | ≤6 words, unchanged — micro-bridge from prior segment. |
| Story lead | ~20% | Drop straight into the news item — who/where/what. No "here's a story about X" announcement. |
| Development | ~55% | What happened, why it matters, one vivid detail. |
| Takeaway | ~25% | Land it. Implicit tie to the listener's domain permitted ("the kind of story that travels well in photography circles"). Explicit bridge forbidden ("since you've been watching X, here's Y"). |

`target_words = round(suggested_length_sec × WORDS_PER_MIN / 60)` — same Prompt A constant, same pacing telemetry (`producer.segment.pacing_measured`), same ceiling semantics. `claim_kind` directives still bound temporal framing in the takeaway: a `neutral` pitch's takeaway can't assert "lately", a `rising` pitch's takeaway can.

**Source-recitation rule:** listener's channel names, video titles, and any `source_refs` proper nouns are NOT spoken inside the story body. That is the implicit/explicit line — the topic is the shared ground, the listener's data is not.

**Per-agent provenance** (from `producer/script.py` SYSTEM_PROMPT) stays intact:
- `youtube` — listener's own taste. Takeaway may use second person ("you") sparingly.
- `alices` — external curator (@AlicesLens). Takeaway uses third person ("Alice"). Never "you've been into X" for a alices segment.

## Implementation surface

Changes are local to `producer/script.py` with one small addition to `producer/__init__.py` for the cache dir constant.

### `producer/script.py`

- **`SYSTEM_PROMPT`** — rewrite for YouTube/Alices segments: new narration contract, web_search usage rules, query-derivation rules (title-only, no listener proper nouns in query), fallback behavior, source-recitation rule. Weather/calendar are already handled by the opener (not routed through this prompt), so the existing provenance tables stay but the hook-vs-data layering sections for taste agents change meaning.
- **`generate_segment`** — add the `web_search` tool to the `messages.create` call (tool block with `type: "web_search_20250305"`, `max_uses: 2`); bump timeout to `40s`; wrap with cache read/write; emit `producer.segment.research_fallback` when the LLM reports no useful results (via a new JSON field `research_outcome: "story" | "hook_fallback"` on the SegmentScript response, stripped before yield).
- **Module docstring** — note research narration is LLM-only by design.

### `producer/__init__.py`

- Add `DEFAULT_CACHE_DIR` constant + `cache_dir()` accessor reading `RADIO_CACHE_DIR` env, mirroring the existing `words_per_min()` pattern.

### New internals (not exported)

- `_segment_cache_path(agent, title, date, wpm) -> Path` — slug + date + wpm key.
- `_read_cached_segment(path) -> SegmentScript | None` — reads the artifact and returns `artifact["segment"]`; soft-fail: logs and returns None on any parse/IO error.
- `_write_cached_artifact(path, segment, debug) -> None` — atomic write (tmp + rename); writes the full `{segment, debug}` artifact.

### Telemetry

- `producer.segment.research_fallback` — `{index, agent, pitch_title, reason}`.
- `producer.segment.cache_hit` — `{index, agent, pitch_title, cache_path}`.
- `producer.segment.cache_written` — `{index, agent, pitch_title, cache_path}`.
- `producer.segment.pacing_measured` — unchanged.

## Invariants preserved

- `cannot-drop-segments` in `stream_episode_script` — hook-narration fallback ensures every input pitch produces an output segment in v0.
- First-segment `segue_in` empty when `is_first=true`.
- `_MIN_SCRIPT_CHARS = 20` floor on script body — hook-narration fallback MUST still produce ≥20 chars.
- `claim_kind` directives bound takeaway temporal framing.
- `target_words` ceiling semantics from Prompt A.
- Memory-isolation invariant: the segment LLM does not read raw `AgentMemory`, and `ProducerMemory` is not passed to this prompt.

## Non-goals (v0)

| Non-goal | Why |
|---|---|
| Queued backup pitches + drop-on-exhaustion | v1. Pencilled under Open questions. |
| `search_seed: str` on the Pitch protocol | Only if `title`-only queries prove too generic in telemetry. |
| Per-episode research budget counter | 2 content segments is small enough that `max_uses=1` per call is sufficient. |
| Allowlisted news sources / RSS feeds | Coverage narrower than LLM-curated search; no current evidence we need this. |
| Cross-segment topic dedup via search | Rare overlap at 2 content segments; add if telemetry shows it matters. |

## Open questions

- **v1: queued pitches + drop-on-exhaustion.** When agents emit 3-5 ordered pitches per guaranteed slot and `stream_episode_script` retries the next pitch on research failure, the `cannot-drop-segments` invariant relaxes to "drop only when the queue is exhausted". Spec this alongside the queue mechanism.
- **Query-quality escalation.** If `title`-only queries produce >30% broadened-retry rate in telemetry, promote `search_seed: str` onto the `Pitch` protocol; agents fill it from richer context (channel/topic hybrid). Decide after 2 weeks of real episodes.
- **Cache key granularity.** `(agent, title_slug, date, wpm)` assumes one episode per user per day. If we add multiple runs per day with different briefs (e.g., morning vs. evening), either add brief-hash to the key or scope cache per-episode.

## Success criteria

- YouTube/Alices segments consistently open with a story lead — no "here's a story about X" announcement, no explicit bridge to listener data.
- `producer.segment.research_fallback` rate measurable in production telemetry; <30% indicates healthy research coverage.
- `producer.segment.cache_hit` works — second same-day run of the same pitch returns identical `SegmentScript` in <50ms (no LLM call).
- `DISABLE_LLM=1` continues to raise cleanly at every entry point.
- Existing pacing telemetry (`producer.segment.pacing_measured`) shows no regression in drift vs. the pre-research baseline.

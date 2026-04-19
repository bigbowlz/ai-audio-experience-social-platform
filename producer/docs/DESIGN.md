# Component: `producer`

**Status:** DRAFT (component extract from master design)
**Master doc:** [`~/.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md`](../../../../.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md) — canonical source.
**Reviewed:** 2026-04-13 (spec review 6/10, red-team)

## Purpose

The Producer is a distinct component (not a prompt pretending to be one) that:

1. Reads internal agent pitches
2. Decides to invoke an external agent (v0 policy: always-invoke)
3. Queries the marketplace stub (hardcoded candidates)
4. Triggers agentic payment via the `payment` component
5. Reads the external agent's pitches
6. Runs `select()` over the full pitch pool, producing a `RunningOrder`
7. Runs the script surface — `generate_opener()` fuses greeting + weather + calendar into one ~75s spoken opener in a single LLM call; `stream_episode_script()` then emits per-segment `SegmentScript`s via async iterator for the remaining content pitches (alices, youtube); `generate_sign_off()` closes the episode. `generate_episode_script()` is the sync back-compat collector. Each content segment is one `generate_segment()` call.
8. Owns Producer-memory (pacing preferences; independent of domain-agent memory)

The CLI and future api-storage consume the above via the shared `pipeline.py` module at repo root, which splits the selected pitches via `split_opener_inputs()` and composes `generate_opener` → `stream_episode_script` (streamed into `audio.generate_episode_audio`) → `generate_sign_off`.

## Key premises

- **P8** Live gen first, cached fallback (with honest labeling)
- **P9** Producer is distinct component; memory-blind by design (invariant)
- **P10** External-agent invocation + agentic payment is Producer's call, not user's
- **P13** Streaming segment TTS → `stream_episode_script()` emits per-segment not monolithic

## Interface contract

```python
class Producer:
    def decide_external_invocation(pitches: list[Pitch]) -> ExternalDecision:
        """v0: always invokes. Returns {decision: 'invoke', rationale: str}.
        v1: conditional on topic-cluster entropy / cocoon detection."""

    def query_marketplace() -> list[CreatorAgentListing]:
        """v0: reads hardcoded list. Returns candidates with handle + price_usdc + wallet."""

    def select_external(candidates: list[CreatorAgentListing], brief: Brief) -> CreatorAgentListing:
        """v0: picks @GoddamnAxl (only listing that matches seed topics)."""

    def select(pitches_by_agent: dict[str, list[Pitch]], brief: Brief) -> RunningOrder:
        """Picks subset fitting total duration, allocates per-agent airtime, orders.
        Sees only the scalar `priority` on each Pitch (memory-isolation invariant)."""

    # NOTE: the script surface (below) lives as module-level async functions in
    # producer/script.py — not methods on a Producer class. Shown here in the same
    # pseudocode block for interface-contract clarity.

# producer/script.py (module-level async functions)

def split_opener_inputs(
    selected: list[Pitch],
) -> tuple[Pitch | None, Pitch | None, list[Pitch]]:
    """Pure split: (weather_pitch, calendar_pitch, content_pitches). Weather and
    calendar fuse into the opener; content pitches go through stream_episode_script."""

async def stream_episode_script(content_pitches: list[Pitch], brief: Brief) -> AsyncIterator[SegmentScript]:
    """Emits content segments one at a time via async iterator. Segment 0 is critical-path
    under P13 (first audio within ~6–10s); segments 1..N stream in the background
    while segment 0 plays. Emits `script.segment.done` SSE per segment and enforces
    cannot-drop-segments at the end. Weather and calendar are NOT passed here —
    they are fused into generate_opener."""

async def generate_segment(segment: Pitch, brief: Brief, is_first: bool) -> SegmentScript:
    """Single structured-output LLM call producing one SegmentScript with a tight
    JSON schema (less room for malformed retry than a monolithic episode call).
    Only called for content pitches (alices, youtube). The call carries a
    server-side `web_search_20250305` tool (`max_uses=2`) so the LLM researches a
    real-world story on the pitch's topic inside the same round-trip; falls back
    to hook-narration if both searches are empty. Wrapped by a same-day on-disk
    cache — hits short-circuit the LLM entirely. See §News-narration below."""

async def generate_opener(
    weather_pitch: Pitch | None,
    calendar_pitch: Pitch | None,
    first_content_pitch: Pitch,
    brief: Brief,
) -> str:
    """Single LLM call: ~75s spoken opener fusing warm greeting + today's weather +
    today's calendar shape + transition into the first content segment. Weather
    and/or calendar may be None; the prompt degrades gracefully. Replaces the
    separate cold_open + weather-segment + calendar-segment trio that previously
    produced visible repetition in the first ~90s."""

async def generate_sign_off(brief: Brief) -> str:
    """Separate small LLM call — ~12s spoken sign-off with a close beat
    ("that's today's feed") + parting line. See
    docs/specs/2026-04-19-prompt-and-cli-polish.md §P8."""

def generate_episode_script(selected: list[Pitch], brief: Brief) -> EpisodeScript:
    """Sync back-compat collector. Runs `split_opener_inputs`, `generate_opener`,
    drains `stream_episode_script` over content pitches, and runs `generate_sign_off`."""

# Public payload builders — single source of truth for the LLM user-message
# JSON shape. Used by generate_opener / generate_segment / generate_sign_off
# AND by storage.episode_artifacts so on-disk records match what the LLM saw.
def build_opener_payload(weather_pitch, calendar_pitch, first_content_pitch, brief) -> dict: ...
def build_segment_payload(segment, brief, is_first) -> dict: ...
def build_sign_off_payload(brief) -> dict: ...
```

**RunningOrder shape:** see master.
**ExternalDecision + CreatorAgentListing:** new shapes; document in this file when implemented.

## Dependencies on other components

| Component       | Contract                                                         | Direction |
| --------------- | ---------------------------------------------------------------- | --------- |
| `agents`        | consumes `list[Pitch]` with `priority: float` scalar only        | in        |
| `payment`       | calls `payment.initiate_tx(producer_wallet, agent_wallet, 0.10)` | out       |
| `audio`         | emits per-segment scripts for streaming TTS                      | out       |
| `learning-loop` | reads Producer-memory; writes Producer-memory at session end     | in/out    |
| `api-storage`   | emits SSE events for every stage                                 | out       |

## Build plan touchpoints

- **Day 1:** Stub Producer. `select()` picks top-N by priority fitting total_length_sec budget. Script generation emits segments as one structured-output call (monolithic `write_script()`, historical). CLI prints valid RunningOrder JSON. End-to-end works.
- **Day 4:** External-invocation decision (unconditional), marketplace stub (hardcoded candidates), `select_external()` → @GoddamnAxl. Wire payment call between internal pitches and external pitch.
- **Day 5 (STRETCH):** Producer-memory inter-agent weights (`agent_weights` driven by like/replay/skip, see §Producer-memory learning rule). Refactor script generation to per-segment async iterator (`stream_episode_script()`) for P13 streaming.

## Success criteria

- Day 1: valid `RunningOrder` JSON end-to-end, coherent script
- Day 4: `decide_external_invocation()` fires, payment call triggered, external pitch factored into final running order on the same `priority` axis (no forced opening, no priority boost)
- Day 5: Episode B reorders running order in at least one visible way vs. Episode A (e.g., a boosted agent wins a bonus slot that an unweighted run would have lost, or a demoted agent loses a bonus slot it previously held)

## Reviewer concerns

### 1. `write_script()` monolithic call risks Minute 6-8 beat (severity: CRITICAL) — B-1

The original `write_script()` interface from master was one structured-output LLM call. On Monday LLM load + one malformed-JSON retry, Episode B live regen blows the 75-sec budget. Screen sits on "writing script..." while builder narrates over silence. The whole "compounding personalization" beat dies.

**Fix (Day 5 morning, hard requirement — as planned):**

- Split the monolithic script call into a per-segment async iterator (`stream_episode_script()`)
- Segment 0 is critical-path under P13 (target ~3-5s LLM + ~3-5s TTS = ~6-10s to first audio)
- Segments 1-N stream in background while segment 0 plays
- Each segment is its own structured-output call (`generate_segment()`) with a tight JSON schema (less room for malformed retry)
- Cold open and sign-off are split out into their own small LLM calls (`generate_cold_open()`, `generate_sign_off()`), sized ≤400 tokens each

**Resolved 2026-04-17:** implemented as `stream_episode_script(...) -> AsyncIterator[SegmentScript]` in [`producer/script.py`](../script.py); per-segment LLM calls via `generate_segment()`; `generate_cold_open()` / `generate_sign_off()` as separate small calls; `generate_episode_script()` retained as sync back-compat collector. First-segment critical-path emits `script.segment.done` SSE event for audio handoff. Composed end-to-end by the shared [`pipeline.py`](../../pipeline.py) module at repo root (CLI + future api-storage). See `docs/specs/2026-04-17-producer-alignment-plan.md` Phase 3.

**Revised 2026-04-18 — opener fusion.** `generate_cold_open()` + weather-segment + calendar-segment collapsed into a single `generate_opener()` LLM call (~75s, one prompt, one response). Rationale: three separate LLM passes over context framing (greeting + weather + calendar) produced audible repetition in the first ~90 seconds. Fusion cuts one LLM round-trip off the critical path, eliminates the repetition, and preserves the P13 first-audio budget. Selection (Step 1 / Step 1.5) is unchanged — weather and calendar still appear in guaranteed slots and flow through `apply_producer_memory` + `select_guaranteed_slots`; they are simply split out of the running order at the script-generation boundary via `split_opener_inputs()` and fed to `generate_opener` instead of `stream_episode_script`. See §Opener fusion below.

### 2. Episode B has NO pre-cached fallback (severity: CRITICAL) — B-1

Episode A has a full auto-switch fallback (pre-cached SSE replay + cached MP3 + pre-captured payment tx). Episode B has nothing. If Minute 6-8 budget breaches, there's no safety net.

**Fix (Day 5 afternoon, hard requirement):**

- Pre-capture an Episode B SSE replay during Day 5 rehearsal (fully scripted running order change, matching pre-captured Episode A)
- Pre-render the stacked-bar SVG comparing Episode A vs. Episode B running orders (reads from two saved JSONs)
- Document a 60-sec budget alarm that auto-plays the pre-cached Episode B opener with honest "pre-recorded" badge
- Episode B fallback parity with Episode A is non-negotiable for the demo

### 3. Episode B budget ambiguous (severity: medium) — A-Feasibility

Master doesn't say whether Episode B includes external invocation + payment (~+6s) or is internal-only regen.

**Spec (v0 decision):** Episode B is **internal-only regen** in v0. External agent is invoked ONLY for Episode A. Producer reads the prior-session external pitch from `agent_memory` (treated as cached external context) and does NOT re-pay. This keeps the 75-sec budget honest. If a future demo needs external-in-B, bump budget to 90s and add a "pre-paid external cache" optimization.

### 4. Producer-memory scope (severity: medium) — A-Scope [REVISED 2026-04-17]

**Previous decision (2026-04-13).** Ship `opener_agent_preference` only — exponential smoothing α=0.3 on a pacing preference.

**Revised decision (2026-04-17).** Pacing (opener, length, fatigue, segment-count preferences) is v1. **v0 ProducerMemory holds inter-agent weights only**, driven by user interaction signals (like / replay / skip). Rationale: pacing changes are invisible until they're wrong, and hard to attribute to any one user signal. Inter-agent weights driven by per-segment feedback form a tight, legible loop — skip calendar → calendar loses bonus-slot competitiveness next episode → visible, attributable behavior change.

**Scope boundary (invariant).** ProducerMemory holds INTER-agent weights only. Intra-agent weights (`profile_state`, `topic_multiplier`, superseded `entity_scores` / `topic_scores`) live in `AgentMemory` and stay inside the agent that owns them. Moving any intra-agent signal into ProducerMemory breaks the marketplace invariant — Producer must remain memory-blind to agent-level state (P9).

**Deterministic application invariant.** ProducerMemory is applied via a pure function BEFORE selection, never passed as free-form input into any LLM prompt. See §Producer-memory learning rule (v0) below.

### 5. `priority` formula + memory-isolation invariant (severity: low, doc-clarity) — A-Clarity

Producer must NEVER read raw agent memory fields (`profile_state`, `topic_multiplier`, or the superseded `entity_scores` / `topic_scores`). Sees only the scalar `priority: float` on each Pitch. See `agents/docs/DESIGN.md` Reviewer Concern #1 for the formula. Enforced by code review; no programmatic check needed in v0.

## Opener fusion (2026-04-18)

The first ~90 seconds of the episode — cold open, weather, calendar — are all
"context framing". Three separate LLM calls produced audible repetition
("good morning, Alice" / "morning — it's 50°F and rainy" / "you have three
meetings today") because each pass had to re-establish orientation from scratch.
Fusing them into one call collapses the greeting, weather beat, and calendar
beat into a single ~75-second spoken passage with natural flow.

### Scope boundary

The fusion is **script-generation-only**. Selection (Step 1 / Step 1.5) is
unchanged:

- `apply_producer_memory` still scales weather and calendar priorities via
  `agent_weights`.
- `select_guaranteed_slots` still emits weather and calendar as guaranteed.
- `select_bonus_segments_llm` still sees weather/calendar as guaranteed slots
  in its budget accounting.
- The `RunningOrder` still honestly represents "weather and calendar are in
  this episode" — they are rendered inside the opener, not dropped.

The split from running-order-to-opener-inputs happens at the
`pipeline.run_episode_pipeline` / `generate_episode_script` boundary via the
pure function `split_opener_inputs()`. Only content pitches (alices,
youtube, future marketplace agents) reach `stream_episode_script`.

### Budget

No changes to `segments.py` constants. `select_guaranteed_slots` still deducts
weather (`45s`), calendar (`30s`), and cold_open (part of `OPEN_CLOSE_SECS =
25s`) from the bonus budget. The actual spoken opener targets
`_OPENER_DURATION_SEC = 75s` — leaving modest slack (~12s vs. the 87s reserved
for cold_open + weather + calendar) which is absorbed into natural variance
and occasional bonus-slot headroom. If opener pacing drift telemetry shows
consistent under-run, tighten the reservation in a follow-up.

### Post-hoc validation

`generate_opener` enforces a single minimum-length check
(`_MIN_OPENER_CHARS = 200`) — roughly 40 words, ~15 seconds at 130 wpm. Any
shorter output is a parse artifact or broken LLM response, raised as
`ValueError`. Word-level accuracy of weather/calendar facts is **not**
validated — same honest posture as the former `generate_cold_open` path: the
LLM is trusted to use the structured `data` fields it receives.

### Graceful degradation

`weather_pitch` and `calendar_pitch` may be `None` (e.g., user without calendar
OAuth, weather API failure bubbled up as `thin_signal` but pitch still emitted).
The opener prompt conditionally skips absent beats. When both are absent,
`generate_opener` degrades to a greeting + transition — roughly the old
cold-open behavior, but at the 75s duration target.

Future non-v0 context agents (e.g., a "news ticker") could either opt into the
opener via `split_opener_inputs` extension (add to the split predicate) or
stay as standalone content segments. Decision deferred until such an agent
arrives.

## News-narration (2026-04-18)

YouTube and alices segments no longer narrate patterns in the listener's
own data ("you've been into underwater photography…"). Instead, `generate_segment`
issues Anthropic's server-side `web_search_20250305` tool inside the same
`messages.create` call, researches a real-world story in the pitch's topic
area, and writes the narration from the search results. The pitch's hook /
`data` / `source_refs` are topical anchors — they shape what to search, they
are not narration material. Full rationale: [docs/specs/2026-04-18-producer-news-narration-design.md](../../docs/specs/2026-04-18-producer-news-narration-design.md).

### Architecture

- **One round-trip per segment.** Tool block `type: "web_search_20250305"`,
  `max_uses=2`. The LLM issues the primary query (derived from `title`; never
  from listener proper nouns in `source_refs`), optionally broadens once, and
  writes the script — all in one `messages.create`. Segment timeout bumps
  30s → 40s to absorb two search round-trips.
- **Narration contract.** Internal beats (never announced): segue →
  ~10% lead → ~70% factual body → ~10% flex band → ~10% takeaway.
  `claim_kind` directives still bound temporal framing in the takeaway.
  `target_words` ceiling semantics from Prompt A unchanged;
  `producer.segment.pacing_measured` unchanged. The "factual body"
  band requires ≥4 distinct factual sentences (named people, works,
  places, dates, numbers, quotes, causes/effects); commentary stays in
  the flex band and takeaway only. See
  [docs/specs/2026-04-19-prompt-and-cli-polish.md §P5](../../docs/specs/2026-04-19-prompt-and-cli-polish.md#p5--beat-ratios-rewritten-for-80-factual-body).
- **Inline markup stripped.** `_strip_inline_markup` runs on every output
  path (cached read, LLM parse, JSON repair, hook fallback, opener,
  sign-off) to remove `<cite index="…">…</cite>` and `<br>` tags that
  leak from `web_search` results. See
  [docs/specs/2026-04-19-prompt-and-cli-polish.md §P7](../../docs/specs/2026-04-19-prompt-and-cli-polish.md#p7--strip-cite--br-tags).
- **Bilingual handling.** Chinese / Japanese proper nouns, titles,
  phrases, and quotes stay in their original script — no translation,
  pinyin/romaji, or parenthetical glosses. Enforced in all four prompts;
  ElevenLabs `eleven_turbo_v2_5` auto-detects CJK. See
  [docs/specs/2026-04-19-prompt-and-cli-polish.md §P3](../../docs/specs/2026-04-19-prompt-and-cli-polish.md#p3--preserve-chinese--japanese-script-verbatim).
- **Source-recitation rule (invariant).** Listener channel names / video
  titles / `source_refs` proper nouns never appear inside the story body.
  The pitch topic is the shared ground; the listener's data is not.

### Hook-narration fallback (preserves `cannot-drop-segments`)

When both searches return nothing usable, the LLM writes the narration from
the pitch `hook` / `source_refs` / `data` in the pre-research voice. The
segment still airs — v0 has no queued-backup-pitch mechanism yet, so the
drop-on-fail path is unsafe. Hit-rate telemetry comes from an OBSERVED
signal, not LLM self-report: `producer.segment.research_fallback` SSE fires
iff the primary response contained zero `server_tool_use` blocks for
`web_search` (reason: `"no_search"`). `_MIN_SCRIPT_CHARS = 20` floor is
enforced on every path (cache hit, story, hook fallback, parse fallback)
via `_validate_segment`.

**Generic-trend counts as nothing-usable.** The SYSTEM_PROMPT now lists a
specific failure mode — think-piece content ("audiences are embracing X",
"several forces are aligning") with no named people, works, dates,
places, or numbers — as grounds to fall back. A hook-narration segment
with real named facts beats a research segment of think-piece vapor. See
[docs/specs/2026-04-19-prompt-and-cli-polish.md §N3](../../docs/specs/2026-04-19-prompt-and-cli-polish.md#n3--title-shape-rule--generic-trend-fallback).

### Parse-failure fallback (defense in depth)

The LLM is instructed to return a single JSON object. When it returns
syntactically invalid JSON (unescaped quotes in a string, stray prose
wrappers, etc.), `generate_segment` recovers along three layers:

1. **Tolerant extraction** — `_extract_json_object` brace-scans the raw
   text, ignoring string bodies, so prose wrappers like `"Here's:\n{...}"`
   parse cleanly.
2. **Repair retry** — one no-tools `messages.create` call whose system
   prompt is "fix JSON syntax, return the corrected object only". The
   raw original text is dumped to
   `$RADIO_CACHE_DIR/segment_scripts/_failed_{slug}_{ts}.txt` for
   postmortem first.
3. **Hook-narration call** — a plain-prose `messages.create` call (no JSON,
   no tools) using `_HOOK_FALLBACK_SYSTEM_PROMPT`. The returned text is
   wrapped into a `SegmentScript` in code with `segue_in=""` when
   `is_first=true` else `"Meanwhile,"`. This keeps
   `cannot-drop-segments` intact even when the model produces nothing
   JSON-shaped.

`producer.segment.parse_fallback` SSE fires whenever layer 2 or 3 ran,
with `variant: "repaired" | "hook_narration"` and the original
`parse_error` string.

### Same-day cache

One pretty-printed JSON artifact per successful segment at
`$RADIO_CACHE_DIR/segment_scripts/{agent}_{title_slug}_{YYYYMMDD}_{wpm}.json`
(default `tmp/segment_script_cache/`, override via `RADIO_CACHE_DIR`). Artifact
shape: `{segment, debug}` where `debug` carries
`{search_tool_calls, search_queries, fallback_path, raw_llm_text,
input_pitch, target_words, words_per_minute}`. `search_tool_calls` and
`search_queries` are observed from the response's `server_tool_use` blocks
(ground truth, not LLM self-report). `fallback_path` is `null` on the
happy path, `"repaired"` when JSON repair salvaged a malformed response,
`"hook_narration"` when both salvage layers failed and the prose-only
fallback produced the segment. The debug block is the manual inspection
surface for live-LLM test runs (`RADIO_CACHE_DIR=tmp/test_outputs/
RUN_LIVE_LLM=1 pytest tests/test_segment_live.py`).

Cache is advisory: corrupted / malformed files are logged and treated as a
miss (`_read_cached_segment` soft-fails). Writes are atomic (tmp + `os.replace`).
Write failures log and continue — they never block generation. TTL is implicit
in the date key.

Hits emit `producer.segment.cache_hit` and return
`artifact.segment` verbatim with no LLM call. Writes emit
`producer.segment.cache_written`. `DISABLE_LLM=1` continues to raise
`RuntimeError` at the entry point (research narration is LLM-only by design);
the cache hit path checks the env before returning.

### Scope boundary

Changes are local to [producer/script.py](../script.py) plus the
`DEFAULT_CACHE_DIR` / `cache_dir()` accessor in
[producer/**init**.py](../__init__.py). Selection (Step 1 / Step 1.5), the
opener, the sign-off, agents, and `stream_episode_script` ordering /
cannot-drop-segments checks are all untouched. Weather and calendar still
flow through the separate opener prompt (no web_search).

### v1 follow-up (pencilled)

Queued backup pitches (3–5 per agent, ordered). The script loop retries the
next pitch on research failure and drops the slot only when the queue is
exhausted — at which point the `cannot-drop-segments` invariant relaxes to
"drop only when queue exhausted". The hook-narration fallback stays as a
safety net even then.

## Producer-memory learning rule (v0)

> **v0 learning-loop stub (2026-04-18).** Everything below specifies the **reader + writer contracts** for Producer memory. The **writer side is stubbed in v0** — `learning-loop` does not call `apply_signal()` or `decay_agent_weights()` at session-end, does not ingest `/react` events, and does not emit `memory.update.*` SSE. The **reader side ships and stays real**: `apply_producer_memory()` is applied pre-selection, `producer.memory.applied` fires silently for bootstrap-fresh users (empty `agent_weights`), and `emit_memory_applied()` stays wired.
>
> **Demo path:** to show Episode B reordering by "learned" weights, pre-seed `agent_weights` via `learning_loop.seed_producer_memory(user_id, {"youtube": 1.5, "calendar": 0.8})` before Episode B. This exercises the reader pipeline end-to-end (including the `producer.memory.applied` SSE event) without a live learning path.
>
> **Writer primitives remain in this module** (`apply_signal`, `decay_agent_weights`) unchanged — they are pure functions, unit-tested, and the only caller that moves them is `learning-loop`, which stays stubbed. See `learning_loop/docs/DESIGN.md` §v0 stub contract.

### Design axes

Every ProducerMemory field must be designed against three questions. A field that fails any of them does not belong in ProducerMemory:

1. **What signal writes it?** (Concrete user interaction; no "we might learn this someday".)
2. **What pure function maps it onto `priority` / length override / eligibility?**
3. **What unit-test fixture shows the before/after?**

### Pipeline position

```
raw pitches_by_agent
  → apply_producer_memory(pitches, memory)    # pure function; no I/O, no LLM
  → adjusted pitches_by_agent                 # priority mutated per agent_weights
  → select_guaranteed_slots(adjusted)         # Phase 1 — one per agent (guarantee invariant)
  → select_bonus_segments_llm(adjusted, …)    # Step 1.5 — LLM sees adjusted priority scalars only
```

The LLM in Step 1.5 never receives `producer_memory` as a raw dict. If a future signal needs LLM attention ("@youtube is the boosted agent this episode"), it is passed as a single structured hint in the prompt, not the underlying memory.

### Shape (v0)

```python
class ProducerMemory(TypedDict):
    schema_version: int                  # = 1 for v0
    agent_weights: dict[str, float]      # agent_name → priority multiplier
    updated_at: str                      # ISO 8601


def bootstrap_producer_memory() -> ProducerMemory:
    """Default ProducerMemory for a user that has never had a row.

    Mirrors `bootstrap_memory()` in agents/protocol.py. Lazy-created
    in-memory so the orchestrator can proceed without a DB round-trip;
    persisted on first real write by the learning-loop.
    """
    now = datetime.now(timezone.utc).isoformat()
    return ProducerMemory(schema_version=1, agent_weights={}, updated_at=now)
```

`agent_weights[agent]` defaults to `DEFAULT_AGENT_WEIGHT = 1.0` when absent. Clamped to `[AGENT_WEIGHT_MIN, AGENT_WEIGHT_MAX] = [0.3, 2.0]`. The guaranteed-slot invariant (one segment per agent) is structural and independent of weight, so an under-weighted agent still appears every episode — weight only affects bonus-slot competitiveness.

`load_producer_memory(user_id)` (learning-loop surface) returns the stored row or `bootstrap_producer_memory()` when the user has no row yet. `apply_producer_memory()` is safe against an empty `agent_weights` dict via its internal `.get(..., DEFAULT_AGENT_WEIGHT)` default — first-time users get identity behavior without a special code path.

### Writer signals (learning-loop)

| Signal   | Source                          | Rule (per event, pre-clamp) |
| -------- | ------------------------------- | --------------------------- |
| `like`   | listener taps like on a segment | `w[agent] *= 1.10`          |
| `replay` | listener replays a segment      | `w[agent] *= 1.20`          |
| `skip`   | listener skips a segment        | `w[agent] *= 0.90`          |

After each event, clamp to `[AGENT_WEIGHT_MIN, AGENT_WEIGHT_MAX]`. Multiplicative updates are commutative within a session, so order of feedback events within one episode does not affect the final weight. Writes are idempotent per `(episode_id, segment_index, signal)` — learning-loop dedupes before applying. Constants (1.10, 1.20, 0.90) are chosen so ~10 consecutive likes saturate the upper clamp and ~10 skips saturate the lower clamp — roughly 2–3 episodes to register a strong preference.

> **v0 stub:** `learning-loop` does not call `apply_signal()` in v0. `agent_weights` stays `{}` (bootstrap) across episodes unless seeded.

#### Per-episode decay (added 2026-04-17, eng review)

After each episode's feedback has been applied, the learning-loop pulls every agent weight toward the neutral default:

```python
EMA_DECAY_ALPHA = 0.05   # 5% pull toward 1.0 per episode

def decay_agent_weights(memory: ProducerMemory) -> ProducerMemory:
    """End-of-episode decay — applied AFTER like/replay/skip signals land."""
    decayed = {
        agent: (1.0 - EMA_DECAY_ALPHA) * w + EMA_DECAY_ALPHA * DEFAULT_AGENT_WEIGHT
        for agent, w in memory["agent_weights"].items()
    }
    return {**memory, "agent_weights": decayed,
            "updated_at": datetime.now(timezone.utc).isoformat()}
```

Without decay the update rule is a one-way ratchet: `like × 1.10` and `skip × 0.90` are not inverses (1.10 · 0.90 = 0.99 — mild residue), and once a weight saturates at 2.0 it takes ~18 consecutive skips to reach 0.3. Real users' preferences drift faster than that. A 5% pull toward 1.0 per episode gives a half-life of ~14 episodes (log(0.5)/log(0.95) ≈ 13.5) — a user who loved @youtube for a week then stopped engaging returns to neutral within a couple of weeks of non-reinforcement. Signals still aggregate within an active interest because the decay is proportional to distance from 1.0, so weights above 1.0 stay above 1.0 as long as likes keep coming.

Decay is applied by the learning-loop, once per episode, after the last feedback event for that episode. It does NOT happen inside `apply_producer_memory()` — the reader stays pure.

> **v0 stub:** `learning-loop` does not call `decay_agent_weights()` in v0. The decay contract above defines behavior when the stub is removed.

Signals attribute to the agent that produced the segment, not the topic or content. Topic-level preferences stay inside `AgentMemory.topic_multiplier` and are processed by the agent's own learning-loop — **never** surfaced into `ProducerMemory`.

### Pure function contract

```python
AGENT_WEIGHT_MIN = 0.3
AGENT_WEIGHT_MAX = 2.0
DEFAULT_AGENT_WEIGHT = 1.0

def apply_producer_memory(
    pitches_by_agent: dict[str, list[Pitch]],
    memory: ProducerMemory,
) -> dict[str, list[Pitch]]:
    """Scale each pitch's priority by its agent's ProducerMemory weight.

    Pure function — no I/O, no LLM, no mutation of inputs. Returns a new
    dict with new Pitch objects (priority scaled; all other fields copied).

    Invariants:
      - Within an agent, relative pitch order is preserved (all pitches
        scaled by the same weight → the max-priority pitch stays max).
      - Cross-agent bonus-slot competitiveness shifts with weight.
      - Guaranteed-slot invariant unaffected (one per agent, structural).
    """
    adjusted: dict[str, list[Pitch]] = {}
    weights = memory.get("agent_weights", {})
    for agent, pitches in pitches_by_agent.items():
        w = weights.get(agent, DEFAULT_AGENT_WEIGHT)
        w = max(AGENT_WEIGHT_MIN, min(AGENT_WEIGHT_MAX, w))
        adjusted[agent] = [{**p, "priority": p["priority"] * w} for p in pitches]
    return adjusted
```

### Unit-test fixtures (required before any writer is wired)

Reader — `apply_producer_memory` (pure function):

| Fixture                                    | Setup                                                                                  | Expected                                                                                                                         |
| ------------------------------------------ | -------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| **Default weights**                        | `agent_weights = {}`                                                                   | All priorities unchanged.                                                                                                        |
| **Single agent boosted**                   | `agent_weights = {"youtube": 1.5}`                                                     | youtube pitches scaled 1.5×; others unchanged.                                                                                   |
| **Intra-agent order preserved**            | youtube weight 1.5; priorities `[0.9, 0.7, 0.5]`                                       | Post-adjust `[1.35, 1.05, 0.75]`; relative order unchanged, argmax unchanged.                                                    |
| **Cross-agent bonus reorder**              | youtube weight 1.5, alices weight 1.0; pre: alices pitch 0.7 > youtube pitch 0.5   | Post: youtube 0.75 > alices 0.7 — youtube wins bonus sort.                                                                     |
| **Weight clamped (over MAX)**              | `agent_weights = {"youtube": 5.0}`                                                     | Effective weight = 2.0; priorities scaled 2.0×.                                                                                  |
| **Weight clamped (under MIN)**             | `agent_weights = {"calendar": 0.01}`                                                   | Effective weight = 0.3.                                                                                                          |
| **Weight malformed (negative, NaN, None)** | `agent_weights = {"youtube": -0.5}`, `{"weather": float("nan")}`, `{"calendar": None}` | Clamped to `[MIN, MAX]`; NaN/None treated as default 1.0 (no propagation).                                                       |
| **Demoted agent still guaranteed**         | `agent_weights = {"calendar": 0.3}`, calendar has 1 pitch priority 0.5                 | Pipeline (`apply_producer_memory` → `select_guaranteed_slots`): calendar still appears in guaranteed; its pitch priority = 0.15. |
| **Memory absent**                          | `memory = {}` or missing `agent_weights` key                                           | All priorities unchanged — `.get("agent_weights", {})` defaults cleanly.                                                         |
| **Bootstrap identity**                     | `memory = bootstrap_producer_memory()`                                                 | All priorities unchanged; function returns a dict shape-equal to input under priority comparison.                                |

End-to-end pipeline (the product-visible claim):

| Fixture             | Setup                                                                                                                                                                                                                             | Expected                                                                                                                                                                                                                      |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Bonus-slot flip** | 4 agents with 3 pitches each; pre-adjust, alices' #2 pitch would win the last bonus slot over youtube's #2 pitch by priority. With `DISABLE_LLM=1` to force the deterministic fallback, set `agent_weights = {"youtube": 1.5}`. | After `apply_producer_memory → select_guaranteed_slots → select_bonus_segments_llm` (fallback path), the final running order contains youtube's #2 pitch in the bonus slot, not alices' #2. Guaranteed slots are unchanged. |

Writer — learning-loop (fixtures live in learning-loop test suite; design-locked here):

| Fixture                   | Setup                                                                                                   | Expected                                                                                                                                                                 |
| ------------------------- | ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Saturation boundary**   | Start at `w = 1.0`; apply `like` × 10 (no decay).                                                       | Final `w = 2.0` (clamped on or before the 10th event: `1.10^8 ≈ 2.14` clamps on the 8th event; test asserts clamp is the final value, not the pre-clamp multiplication). |
| **Idempotency**           | Apply `(episode_id=E1, segment_index=2, signal=like)` twice.                                            | Weight updated once; second application is a no-op (dedupe).                                                                                                             |
| **Per-episode EMA decay** | `agent_weights = {"youtube": 2.0, "calendar": 0.3}`; run `decay_agent_weights()` once with no feedback. | youtube: `0.95·2.0 + 0.05·1.0 = 1.95`; calendar: `0.95·0.3 + 0.05·1.0 = 0.335`. Both move one step toward 1.0.                                                           |

Integration — SSE:

| Fixture                                | Setup                                                                                   | Expected                                                                                                                                                                                                                                        |
| -------------------------------------- | --------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **`producer.memory.applied` emission** | `agent_weights = {"youtube": 1.5, "calendar": 0.8}`; pitches with known raw priorities. | One `producer.memory.applied` event fires before `producer.selecting.started`, payload contains `{agent_weights, changes: [{agent, pre_max_priority, post_max_priority}, ...]}`. No event emitted when `agent_weights == {}` (silent identity). |

### Non-goals (v0)

| Non-goal                                                    | Why                                                                                                                   |
| ----------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| Pacing preferences (opener, length, fatigue, segment_count) | v1. Moved out of v0 — see Reviewer Concern #4. Not writable from likes/replays/skips without a separate signal layer. |
| Topic-level or content-level weights                        | Violates scope boundary. See `AgentMemory.topic_multiplier`.                                                          |
| Cross-user / aggregate priors                               | v1+. v0 memory is per-user.                                                                                           |
| LLM-based weight inference / raw memory in prompts          | The pure function is the whole point — behavior must be testable and honest about where the decision happens.         |

### Integration in orchestrator

```python
pitches_by_agent, brief = run_episode(agents, user_id)
producer_memory = load_producer_memory(user_id)            # bootstrap if row absent
raw_pitches_by_agent = pitches_by_agent
pitches_by_agent = apply_producer_memory(pitches_by_agent, producer_memory)
emit_memory_applied_sse(producer_memory, raw_pitches_by_agent, pitches_by_agent)
guaranteed, remaining, budget = select_guaranteed_slots(pitches_by_agent)
bonus, reasons = select_bonus_segments_llm(
    guaranteed, remaining, budget, brief["today_context"],
    # producer_memory no longer in signature — see "SSE: producer.memory.applied" below
)
```

The LLM no longer receives producer memory in any form because ProducerMemory has already been applied. `bonus.py`'s `_format_input` drops `producer_memory` from the payload entirely; the `select_bonus_segments_llm` signature removes the `producer_memory` parameter; system-prompt rule #5 ("Producer memory informs but does not mandate") is deleted — there is nothing for the LLM to interpret.

#### SSE: `producer.memory.applied` (added 2026-04-17, eng review)

> **Implementation status (2026-04-17):** Implemented via `producer/events.py`
> bus + JSONL/stdout sink. `producer.memory.applied`, `producer.selecting.started`,
> `producer.pick`, `producer.selecting.done` are all live. HTTP/SSE transport
> deferred to api-storage; sink swap is the only change required when api-storage lands.
>
> **v0 learning-loop stub (2026-04-18):** `producer.memory.applied` stays silent in v0 for bootstrap-fresh users because `agent_weights` is empty — the identity-transform short-circuit at the top of `build_memory_applied_event` returns `None`. Seeded demos (see the callout at §Producer-memory learning rule) make the event fire even though there is no live learning path.

Deterministic memory application is otherwise invisible in the SSE trace — a calendar-demoted-by-skips episode looks identical to a neutral one. To make the learning loop visible without re-introducing memory into the LLM prompt, emit a single structured event before `producer.selecting.started`:

```python
emit("producer.memory.applied", {
    "agent_weights": producer_memory["agent_weights"],           # {"youtube": 1.5, "calendar": 0.8}
    "changes": [
        {
            "agent": agent,
            "pre_max_priority": max(p["priority"] for p in raw_pitches_by_agent[agent]),
            "post_max_priority": max(p["priority"] for p in adjusted_pitches_by_agent[agent]),
        }
        for agent in producer_memory["agent_weights"]
    ],
})
```

The event fires only when `agent_weights` is non-empty (a bootstrap-fresh user produces no event — the identity transform stays silent). The event is the product's "it learned from last episode" beat rendered for the UI. The raw dict stays out of LLM prompts; this event is for UI/SSE consumers only.

## Pacing (WORDS_PER_MIN)

All spoken-length reasoning inside the Producer flows through a single
conversational-pace constant:

```python
DEFAULT_WORDS_PER_MIN = 130    # producer/__init__.py
def words_per_min() -> int:    # reads PRODUCER_WORDS_PER_MIN env on every call
```

130 wpm is a warm-conversational rate (faster than NPR-slow, slower than
podcast-energetic). Override via `PRODUCER_WORDS_PER_MIN` env — same override
pattern as `PRODUCER_LLM_MODEL` two lines above the constant.

**Enforcement is prompt-only in v0.** Each `generate_segment` payload
carries `target_words = round(suggested_length_sec * wpm / 60)` plus the
raw `words_per_minute`; the segment system prompt tells the model to land
near that word count, treating it as a ceiling. `generate_cold_open` and
`generate_sign_off` do the same against their fixed duration targets
(12s / 10s).

There is no retry loop. Instead `stream_episode_script` emits a
`producer.segment.pacing_measured` event per segment carrying
`{target_sec, target_words, words, measured_sec, drift_sec,
estimated_sec_self_report, words_per_minute}`. Drift data from real
episodes tells us whether a retry is ever needed; if measured drift
stays consistently >25%, escalate to a retry for non-critical-path
segments (segments 1+, never segment 0 under the P13 first-audio budget).

## User profile (cold-open salutation)

`Brief.user_profile: UserProfile | None` carries `first_name` when the
user has completed the Google OAuth flow. The orchestrator loads it from
`~/.config/radio-podcast/user_profile.json` during Brief assembly; auth
writes that file once after `openid profile email` consent (see
`auth/calendar_auth.py`).

The cold-open prompt reads `user_profile.first_name` and addresses the
user by first name at least once when present; falls back to "you"
otherwise. No other Producer surface uses the profile in v0. Agents do
not consume it at all — it's orthogonal to pitch selection.

## Open questions (component-scoped)

- **Tie-breaking in `select()`:** two pitches at identical `priority` — first-emitted wins? agent-index order? random-seed? **Recommended:** deterministic by `(priority DESC, agent_name ASC)` so tests are reproducible.
- **Airtime reconciliation:** if selected pitches sum to ≠ target length, does Producer truncate a pitch or add a filler music transition? **Resolved (2026-04-16):** Producer owns segment lengths via `DEFAULT_SEGMENT_SEC` lookup in `producer/segments.py`. Agents do not set `suggested_length_sec`. `select_segments()` accepts `length_overrides` so Producer memory or user preferences can adjust per-agent defaults. All lengths are clamped to `MAX_SEGMENT_SEC`.

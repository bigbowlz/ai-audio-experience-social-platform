# Component: `learning-loop`

**Status:** STUBBED (v0 demo) — rewritten 2026-04-18 against the locked `AgentMemory` / `ProducerMemory` / `EpisodeSignals` shapes. **No signal ingestion, no memory writes, no `memory.update.*` SSE beats in v0.** The reader side of memory (pure functions that shape `priority` and candidate scores) ships in its owning components; the writer side — everything that turns `/react` signals into memory deltas — is deferred. See §v0 stub contract below.
**Master doc:** [`~/.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md`](../../../../.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md) — canonical source.
**Canonical schemas:** [`agents/youtube/docs/DESIGN.md`](../../agents/youtube/docs/DESIGN.md) §`AgentMemory` schema + §`EpisodeSignals` companion schema. [`producer/docs/DESIGN.md`](../../producer/docs/DESIGN.md) §Producer-memory learning rule.

## Purpose

The cross-component policy layer for memory. When implemented, this component owns:

1. **Signal ingestion** — accept `EpisodeSignals` (emissions written at episode start + reactions appended via `/react` during playback) from api-storage at session-end.
2. **Update rules** — deterministic, per-signal delta rules on `AgentMemory.topic_multiplier` (topic-scored agents) and `ProducerMemory.agent_weights` (Producer).
3. **Session-end batching + clamps** — aggregate all signals from one session into one write per memory record. Apply move caps and per-episode decay.
4. **Memory-isolation invariant** — Producer remains memory-blind. Learning-loop writes `topic_multiplier` into an agent's record but does not let Producer read it. Producer sees only its own `agent_weights` and the scalar `priority` on each `Pitch`.
5. **`memory.update.*` SSE beats** — the on-screen "each agent decided what to learn" panel during the session-end phase.

**v0 ships none of the above as live behavior** (see §v0 stub contract). The reader-side primitives that consume memory (`apply_producer_memory` in `producer/memory.py`, `pitch()`'s `topic_multiplier.get(T, 1.0)` read in `agents/youtube/pitch.py`) ship and stay pure. Memory values stay at their bootstrap defaults — `topic_multiplier == {}` and `agent_weights == {}` — so the reader-side math collapses to the identity transform end-to-end.

## Key premises

- **P9** Agents are real agents with decision authority; per-user memory; memory-isolation invariant.
- **P11** Player IS the telemetry surface — when unstubbed, `/react` signals flow from UI directly into this component.
- Master: "Per-agent objectives", "Memory-isolation invariant", "Update-on-session-end rule".

## v0 stub contract

### What the stub is

`learning_loop/` ships as a thin Python module exposing **no-op session-end hooks** and a narrow seeding entrypoint for demo fixtures. The module imports the locked memory shapes from the components that own them (`agents.protocol.AgentMemory`, `producer.memory.ProducerMemory`) — it does not redefine them. The module does not:

- Open a `/react` endpoint.
- Subscribe to `session.ended`.
- Read from any `signals` table.
- Call `producer.memory.apply_signal()` or `producer.memory.decay_agent_weights()` from any runtime path.
- Write `AgentMemory.topic_multiplier` or `ProducerMemory.agent_weights` from any runtime path.
- Emit `memory.update.started` / `memory.update.decided` / `memory.update.applied` / `memory.update.done` SSE events.

### What the stub does

| Surface                                                   | Behavior                                                                                                                                                                                                                                                                                            |
| --------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `session_end(user_id, episode_id)`                        | No-op. Logs a single line: `learning-loop: session_end stubbed — no memory writes in v0`. Callable from orchestrator / api-storage when they exist, but v0 never calls it.                                                                                                                          |
| `load_agent_memory(user_id, agent_name)`                  | Returns `bootstrap_memory()` from `agents.protocol`. No persistence. Lazy; no DB round-trip.                                                                                                                                                                                                        |
| `load_producer_memory(user_id)`                           | Delegates to `producer.memory.load_producer_memory()`, which already returns `bootstrap_producer_memory()`. No persistence.                                                                                                                                                                         |
| `seed_producer_memory(user_id, agent_weights)`            | **Test/demo seam.** Lets a fixture or env var hand a pre-computed `agent_weights` dict into the module's in-memory cache so the next `load_producer_memory(user_id)` call returns those weights. Enables Episode B's "running-order reordered" demo beat without wiring the full learning pipeline. |
| `seed_topic_multiplier(user_id, agent_name, multipliers)` | Same seam at the `AgentMemory` level. Lets fixtures pre-seed `topic_multiplier` on a per-(user, agent) record.                                                                                                                                                                                      |

**Seeding is a test/demo surface, not production.** When the component is unstubbed, `seed_*` functions get a `DeprecationWarning` and the real signal-ingestion path replaces them. Fixtures that use seeding today will need to migrate to emitting synthetic `EpisodeSignals` records.

### Reader side stays real

The following reader-side code is NOT part of this stub — it ships in its owning components today and continues to work:

- `producer/memory.py` → `apply_producer_memory(pitches, memory)` — the pure function applied pre-selection.
- `producer/memory.py` → `build_memory_applied_event` / `emit_memory_applied` — silent for bootstrap-fresh users; remains silent in v0 because `agent_weights == {}`.
- `agents/youtube/pitch.py` → `combined_topic_scores[T] * topic_multiplier.get(T, 1.0)` read. `topic_multiplier == {}` in v0, so the multiplier term is uniformly 1.0 (identity).

### Why stub, not implement

- **Time constraint.** Implementing the full loop means `/react` endpoint, signals table, 15-second inactivity `session.ended` trigger, per-signal rules across all four agents + Producer, `memory.update.*` SSE panel, Supabase persistence. Days of cross-cutting work.
- **Schema churn risk.** The pre-2026-04-15 version of this doc still referenced `entity_scores` / `topic_scores` / `signal_weights` / `observe()` — shapes superseded by the 2026-04-15 `AgentMemory` lock. Implementing now would have committed to a redesign pass first. Stubbing lets the doc align with the locked shape without the redesign.
- **Reader is already real.** The two product-visible beats — `producer.memory.applied` firing when weights are non-empty, and `pitch()` multiplying candidate scores by `topic_multiplier` — work end-to-end today. Seeding memory via fixture demonstrates both without a live learning path.
- **Writer primitives are already shaped.** `producer.memory.apply_signal()` and `producer.memory.decay_agent_weights()` are pure functions, unit-tested, and unwired. When the stub is removed, wiring them from a session-end trigger is mechanical; no schema work remains.

### Consequence for the demo

- **Episode A → react → Episode B does not auto-reorder in v0.** No `/react` ingestion means no memory delta; Episode B is identical-in-expectation to Episode A unless memory is pre-seeded.
- **Seeded-demo path:** a Day 5 rehearsal script seeds `agent_weights` (e.g., `{"youtube": 1.5, "calendar": 0.8}`) before Episode B and leaves Episode A unseeded. `producer.memory.applied` fires on Episode B only; the running-order shift is real even though the _learning_ step is stubbed.
- **Honest framing:** the demo narration should describe the seeded beat as "this is what memory does with signals we already collected" rather than "this is what just happened from your reactions." The mechanism is real; the closed-loop is v1.

## When unstubbed — the contract

Everything below defines the component when the stub is removed. This is the design the stub is holding open; nothing here is running in v0.

### Signal schema

`EpisodeSignals` and its sub-shapes are locked in [`agents/youtube/docs/DESIGN.md`](../../agents/youtube/docs/DESIGN.md) §`EpisodeSignals` companion schema. Summary:

```python
class PitchEmission(TypedDict):
    segment_index: int
    agent: str                   # "youtube" | "calendar" | "weather" | "alices"
    topic: str | None            # None for non-topic-scored agents (weather/calendar)
    source_refs: list[str]
    priority: float              # priority at which this pitch entered the running order

class ReactionEvent(TypedDict):
    type: Literal["like", "skip", "replay"]
    segment_index: int
    timestamp_ms: int
    segment_position_sec: float  # playhead BEFORE mutation (see Reviewer Concern #4)

class EpisodeSignals(TypedDict):
    schema_version: int
    episode_id: str
    user_id: str
    emissions: list[PitchEmission]
    reactions: list[ReactionEvent]
```

`emissions` is written at episode start by Producer; `reactions` is appended by Player via `/react` during playback. Learning-loop reads both at session-end.

### Memory shapes (owned by other components; not redefined here)

- `AgentMemory` — locked in [`agents/youtube/docs/DESIGN.md`](../../agents/youtube/docs/DESIGN.md) §`AgentMemory` schema. Fields: `schema_version`, `profile_state: InterestProfile` (owned by `agents/youtube`), `topic_multiplier: dict[str, float]` (owned by this component), `updated_at`.
- `ProducerMemory` — locked in [`producer/docs/DESIGN.md`](../../producer/docs/DESIGN.md) §Producer-memory learning rule. Fields: `schema_version`, `agent_weights: dict[str, float]` (owned by this component), `updated_at`.

Learning-loop writes `topic_multiplier` and `agent_weights` only. Everything else is owned by its originating component.

### Update rules

#### `AgentMemory.topic_multiplier` (topic-scored agents)

Per-reaction delta, applied in one batched pass at session-end:

| Reaction                | Rule                                           |
| ----------------------- | ---------------------------------------------- |
| `like` on segment `s`   | `topic_multiplier[emissions[s].topic] *= 1.20` |
| `replay` on segment `s` | `topic_multiplier[emissions[s].topic] *= 1.10` |
| `skip` on segment `s`   | `topic_multiplier[emissions[s].topic] *= 0.85` |

After the batch, clamp: `topic_multiplier[T] ∈ [0.1, 10.0]` per agent spec. Missing keys default to 1.0 (neutral) at pitch-time via `.get(T, 1.0)`.

**Per-session move cap (from master).** After the batched multiplication, clamp the delta for each topic: `|new / old| ∈ [1/1.15, 1.15]`, with a minimum move of ±0.3 in log-space for cold-start (`old == 1.0`). This prevents one angry session from tanking a topic permanently.

**Topic-less segments.** `weather` and `calendar` pitches have `topic is None`. Their reactions do not update `topic_multiplier` (the field is topic-keyed). They still update `ProducerMemory.agent_weights` — see below.

**Only the owning agent's record is touched.** If `segment_agent == "youtube"`, the write lands on `agent_memory(user_id="...", agent_name="youtube")`. No cross-agent influence. Cross-agent signal is a v1 feature.

#### `ProducerMemory.agent_weights` (per-agent bonus-slot weighting)

Per-reaction delta, applied to the agent that produced the segment. Update rules live in [`producer/docs/DESIGN.md`](../../producer/docs/DESIGN.md) §Producer-memory learning rule → §Writer signals — do not duplicate here.

At session-end, after all reactions have been folded in, call `producer.memory.decay_agent_weights(memory)` once to pull every weight toward `DEFAULT_AGENT_WEIGHT = 1.0` by 5%. The pure functions `apply_signal` and `decay_agent_weights` already live in `producer/memory.py`; learning-loop is the only caller.

#### Idempotency

Writes are idempotent per `(episode_id, segment_index, signal)`. Learning-loop dedupes before applying — if the same reaction record lands twice (Player retry, at-least-once delivery), the second application is a no-op.

### Memory-isolation invariant

Unchanged from master. Producer is memory-blind: it sees its own `ProducerMemory.agent_weights` (through the pure function `apply_producer_memory`, applied pre-selection) and the scalar `priority` on each `Pitch`. It **never** reads `profile_state` or `topic_multiplier` from any agent's `AgentMemory`.

Two reasons (from master):

1. **Learning isolation.** YouTube-scope + calendar-scope + weather-scope taste are different objects. Blurring them degrades per-scope signal quality.
2. **Marketplace/social future.** For a domain agent (e.g., `@SarahsTechFilter`) to be independently publishable, its memory cannot be readable by any agent that also sees private-scope memory (calendar, Slack, etc.).

Producer's own `ProducerMemory.agent_weights` is about **pacing / inter-agent competitiveness**, not taste. Two independent learning loops.

### `memory.update.*` SSE beats (when unstubbed)

At `session.ended`:

```
memory.update.started { agent, reasoning_summary }     # one per agent, including Producer
memory.update.decided {
  agent,
  action: "updated" | "no_update",
  deltas: {...},                                          # {} on no_update
  reasoning_summary: str                                  # human-readable, shown in the panel
}
memory.update.applied { agent, final_memory_snapshot }   # optional, for debug/dashboard
memory.update.done    { total_agents_updated, total_no_update }
```

"No update" rows matter as much as update rows — they prove each agent independently decides. Makes per-agent isolation visible to judges. Deterministic no-update fallback on LLM failure for the `reasoning_summary` string (see Reviewer Concern #3) — the panel still renders.

### `session.ended` auto-trigger

Fires **N = 15 sec** of inactivity (no playback, no `/react`, no navigation). Timer resets on any event. Explicit End-session button on player card fires immediately (see `player/docs` Reviewer Concern #4).

### Dependencies on other components

| Component     | Contract                                                                                        | Direction   |
| ------------- | ----------------------------------------------------------------------------------------------- | ----------- |
| `player`      | Emits `EpisodeSignals.reactions` via `/react`                                                   | in          |
| `api-storage` | Persists signals + memory to Supabase; emits `memory.update.*` SSE                              | out         |
| `agents`      | Consumes `AgentMemory` shape (read-only here, except for `topic_multiplier` writes)             | out (write) |
| `producer`    | Consumes `ProducerMemory` shape; provides `apply_signal` / `decay_agent_weights` pure functions | in/out      |

### Build plan touchpoints

- **Day 1–4 (v0):** component stays stubbed. Fixtures and `seed_*` functions drive any demoable memory state.
- **Post-demo (v1):** `/react` endpoint + `signals` table + `session.ended` trigger + update rules + `memory.update.*` SSE wired end-to-end. First real persistence pass. Seed functions deprecated.

### Success criteria (when unstubbed)

- Episode A → react → Episode B shows ≥1 visible shift in running order driven by real signal ingestion (not seeded).
- `memory.update.*` panel renders one row per agent, including "no update" rows.
- Memory survives the CLI restart (Supabase persistence works).
- Writer idempotency holds under duplicate delivery.

## Reviewer concerns (carried from pre-stub doc; still valid when unstubbed)

### 1. Move cap: per-key, not vector-norm (severity: medium) — A-Clarity

Master says "no single session may shift any `topic_multiplier[T]` by more than ±15%" without specifying per-key or across-the-vector. **Fix: per-key.** Vector-norm capping would let one topic gobble the whole budget, defeating the purpose. Per-key matches intent.

### 2. `session.ended` auto-trigger N unspecified (severity: medium) — A-Completeness

Master says "auto-trigger after N seconds of listener inactivity" without specifying N. **Fix: N = 15 sec.** Timer resets on any event. Explicit End-session button fires immediately.

### 3. Memory-update LLM call fallback missing (severity: medium) — A-Completeness

If `memory.update.decided`'s `reasoning_summary` is LLM-generated and the call fails mid-beat, the panel renders nothing. **Fix: deterministic no-update fallback.** On LLM failure, emit `action: "no_update"`, `deltas: {}`, `reasoning_summary: "update service unavailable; memory held"`. The beat still works.

### 4. `segment_position_sec` capture point (severity: medium) — A-Clarity

Paired with `player/docs` Reviewer Concern #3. Capture **before** any playback mutation:

- On skip: playhead at click, BEFORE `audio.currentTime = nextSegmentStart`.
- On replay-15: playhead BEFORE `audio.currentTime -= 15`.
- On `like` (long-press): playhead at the moment of long-press.

Diagnostic value: for skips, `segment_position_sec` separates hook failures (early exits) from fatigue failures (late exits). Wrong capture point corrupts the diagnostic.

### 5. Producer-memory scope (severity: medium) — A-Scope [REVISED 2026-04-17]

v0 `ProducerMemory` holds inter-agent weights only (`agent_weights: dict[str, float]`). Pacing preferences (`opener_preference`, `target_length`, `fatigue_point`, `segment_count`) are v1. Rationale + full update rules: [`producer/docs/DESIGN.md`](../../producer/docs/DESIGN.md) §Producer-memory learning rule.

## Open questions (component-scoped)

- **Same `@entity` in two agents' pitches.** Each agent has independent memory; they track their own signals. If `@pg` shows up in YouTube AND `@GoddamnAxl`, both get updates from their own segment's signals. Expected.
- **Clock drift on per-episode decay.** `decay_agent_weights` runs at session-end — no cron needed. Clean.
- **`topic_tags` provenance on a Pitch.** See `agents/docs` Open Questions (agent-level at pitch-time for domain agents, hand-tagged for `alices_agent`).

# Component: `learning-loop`

**Status:** DRAFT (component extract from master design)
**Master doc:** [`~/.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md`](../../../../.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md) — canonical source.
**Reviewed:** 2026-04-13 (spec review 6/10, red-team)

## Purpose

The cross-component policy layer for memory:

1. **Signal schema** — `EpisodeSignals` shape that the player emits, the `observe()` methods consume
2. **Memory schemas** — per-agent memory (domain + creator) + Producer-memory
3. **Update rules** — per-signal point updates, session-end application, ±15% move cap, 5% weekly decay
4. **Memory-isolation invariant** — Producer is memory-blind by design; sees only the scalar `priority`
5. **`memory.update.*` SSE beats** — the on-screen "each agent decides what to learn" panel

This component is a policy + schema owner. The actual memory writes happen inside each agent's `observe()`, each scoped to that agent's own row. This component defines the rules they all follow.

## Key premises

- **P9** Agents are real agents with decision authority; per-user memory; memory-isolation invariant
- **P11** Player IS the telemetry surface — signals flow from UI directly into this component
- **Learning Loop & Per-Agent Optimization section** (master: "Per-agent objectives", "Memory-isolation invariant", "Update-on-session-end rule")

## Signal schema

```python
class EpisodeSignals(TypedDict):
    episode_id: str
    events: list[SignalEvent]

class SignalEvent(TypedDict):
    type: Literal["skip", "replay", "more_like"]
    segment_index: int
    segment_agent: str
    timestamp_ms: int
    segment_position_sec: float   # see Reviewer Concern #4 for capture point
```

## Memory shapes

```python
# Domain + creator agents
class AgentMemory(TypedDict):
    version: int                               # schema version, start at 1
    signal_weights: dict[str, float]           # {replay: 1.2, skip: -0.8, more_like: 1.8}
    entity_scores: dict[str, float]            # {"@ofmiles": 2.1, ...}
    topic_scores: dict[str, float]             # {"startup_culture": 1.6, ...}
    last_updated: str                          # ISO 8601

# Producer (v0 ships opener_preference only; see Reviewer Concern #5)
class ProducerMemory(TypedDict):
    version: int
    opener_agent_preference: str               # agent with lowest time-to-first-skip when opening
    last_updated: str
    # v1 fields (deferred): target_length_preference_sec, fatigue_point_sec, segment_count_preference
```

## Update rules (applied in each agent's `observe()` at session-end)

Per-signal point updates (applied to aggregate deltas first):

- `more_like` on a segment → `entity_scores[segment.entity] += 1.8`; top 2 topic tags → `topic_scores[tag] += 0.9` each
- `replay` on a segment → `entity_scores[segment.entity] += 1.2`; top topic tag → `topic_scores[tag] += 0.6`
- `skip` on a segment → `entity_scores[segment.entity] -= 0.8`; top topic tag → `topic_scores[tag] -= 0.4`

Then apply move cap + decay:

- **Move cap per-key:** each `entity_scores[k]` and `topic_scores[k]` individually capped at ±15% of its current magnitude, minimum move ±0.3 for cold-start (see Reviewer Concern #1)
- **Clamp:** all scores clamp to `[-5.0, +5.0]`
- **Weekly 5% decay:** stubbed in v0 (noted in memory for v1)

Producer-memory (v0, opener_preference only):
- On session-end, if time-to-first-skip on the opening agent < previous best, update `opener_agent_preference` to that agent (simple argmin, no smoothing in v0).

**Update is per-session, not per-signal.** One cranky Tuesday morning shouldn't tank `@ofmiles` forever.

## Memory-isolation invariant

Producer is memory-blind. It receives each Pitch with a pre-computed scalar `priority` and NEVER reads `entity_scores` / `topic_scores` from agent memory. Two reasons:

1. **Learning isolation:** blurring YouTube-scope + calendar-scope + weather-scope taste degrades per-scope signal quality.
2. **Marketplace/social future (project memory):** for domain agents to be independently publishable — e.g., a user publishes `@SarahsTechFilter` — their memory cannot be readable by any agent that also sees private-scope memory (calendar, Slack).

Producer's own memory is about **pacing**, not taste. Two independent learning loops.

## `memory.update.*` SSE beats

At `session.ended`:

```
memory.update.started { agent, reasoning_summary }          # one per agent, including Producer
memory.update.decided {
  agent,
  action: "updated" | "no_update",
  deltas: {...},                                              # {} on no_update
  reasoning_summary: str                                      # human-readable, shown in the panel
}
memory.update.applied { agent, final_memory_snapshot }       # optional, for debug/dashboard
memory.update.done    { total_agents_updated, total_no_update }
```

"No update" rows are as important as update rows — they prove each agent independently decides. Makes per-agent isolation visible to judges.

## Dependencies on other components

| Component | Contract | Direction |
|---|---|---|
| `player` | emits `EpisodeSignals` via `/react` | in |
| `api-storage` | persists signals + memory to Supabase; emits `memory.update.*` SSE | out |
| `agents` | each agent's `observe()` consumes signals, applies rules to its own memory | in |
| `producer` | Producer-memory updates at session end; read at next Brief | in/out |

## Build plan touchpoints

- **Day 1:** stub memory schema in `agent_memory` Supabase table. Agents load/write stub memory (no real updates).
- **Day 5 STRETCH (Approach B, gated behind `APPROACH_B=true`):** `/react` endpoint writes to `signals` table. Session-end trigger fires update rules on all 4 agents + Producer. `memory.update.*` SSE events fire. Memory Update panel renders in player UI.
- **Hard cut rule:** if memory + regen isn't landing by 2pm Day 5, revert to Approach A; `memory.update.*` SSE panel is dead weight without the regen that shows it working.

## Success criteria

- Episode A → react → Episode B shows **3 visible shifts** in running order:
  1. Pitch priority weighting (different picks or order)
  2. Per-agent time allocation (someone gets more/less seconds)
  3. Opener changes (from one agent to another via Producer-memory `opener_preference`)
- `memory.update.*` panel renders one row per agent including "no update" rows
- Memory survives the CLI restart (Supabase persistence works)

## Reviewer concerns

### 1. Move cap ambiguous: per-key or vector-norm? (severity: medium) — A-Clarity

Master says "no single session may shift any `entity_scores[*]` or `topic_scores[*]` value by more than ±15%" without specifying per-key or across-the-vector.

**Fix:** **per-key.** Each individual score capped at ±15% of its current magnitude, minimum ±0.3. Vector-norm capping would let one score gobble the whole budget, defeating the purpose. Per-key matches the intent.

### 2. `session.ended` auto-trigger N unspecified (severity: medium) — A-Completeness

Master says "auto-trigger after N seconds of listener inactivity" without specifying N.

**Fix:** **N = 15 sec** of inactivity (no playback, no `/react` events, no navigation). Timer resets on any event. Explicit End-session button on player card fires immediately (see `player/docs` Reviewer Concern #4).

### 3. Memory-update LLM call fallback missing (severity: medium) — A-Completeness

If the `memory.update.decided` step calls an LLM to produce the `reasoning_summary` and that call fails mid-beat, the panel renders nothing.

**Fix:** deterministic no-update fallback. On LLM failure for any agent:
```
memory.update.decided {
  agent,
  action: "no_update",
  deltas: {},
  reasoning_summary: "update service unavailable; memory held"
}
```
The panel still shows all agents' rows; the beat still works. Better to honestly show "update service unavailable" than to silently hide.

### 4. `segment_position_sec` capture point (severity: medium) — A-Clarity

Paired with `player/docs` Reviewer Concern #3. Capture **before** any playback mutation:
- On skip: playhead at the moment of the skip click, BEFORE `audio.currentTime = nextSegmentStart`
- On replay-15: playhead BEFORE `audio.currentTime -= 15`
- On more_like: playhead at the moment of long-press

The `segment_position_sec` field is diagnostic — for skips, it separates hook failures (early exits) from fatigue failures (late exits). Wrong capture point corrupts the diagnostic.

### 5. Producer-memory scope (severity: medium) — A-Scope

Master defines 4 Producer-memory fields with exponential smoothing α=0.3. Real work for Day 5 on top of 4 domain-agent memories.

**Fix (Day 5 cut):** ship `opener_agent_preference` only. Simplest possible update rule: argmin time-to-first-skip. Visible in Episode B as a different agent opening. Defer the other 3 fields + smoothing to v1.

### 6. Memory-update wiring gated behind `APPROACH_B=true` (severity: low) — A-Scope

Build scaffolding for `memory.update.*` SSE on Day 3-4 is tempting but eats time. Gate all of it — endpoint writes, SSE events, panel rendering — behind `APPROACH_B=true`. Day 5 flips the flag; if Day 5 cut happens, the flag stays off and the beat is gracefully absent rather than half-wired.

## Open questions (component-scoped)

- **`topic_tags` on a Pitch:** which component emits them? See `agents/docs` Open Questions (agent-level at pitch-time for domain agents, hand-tagged for `alices_agent`).
- **What if the same `@entity` appears in two different agents' pitches?** Each agent has independent memory; they separately track their own entity score. If `@pg` shows up in YouTube AND `@AlicesLens`, both get updates from their own segment's signals. Expected.
- **Clock drift on weekly decay:** v0 is stubbed. v1 needs a cron-like trigger. Defer.

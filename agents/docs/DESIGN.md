# Component: `agents`

**Status:** DRAFT (component extract from master design, reconciled 2026-04-15)
**Master doc:** [`~/.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md`](../../../../.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md) — canonical source; this doc is scoped to the `agents` component only.
**Child specs:**
- [`agents/youtube/docs/DESIGN.md`](../youtube/docs/DESIGN.md) — `InterestProfile`, TF-IDF, K=5 provenance, `pitch()` flow, `AgentMemory` schema (locked 2026-04-15)
- [`agents/docs/prompt_design.md`](prompt_design.md) — hook guardrails (`claim_kind`, `provenance_shape`), today's-context handoff, Producer running-order, `EpisodeScript` output
**Reviewed:** 2026-04-13 (spec review 6/10, red-team); reconciled 2026-04-15 against child specs

## Purpose

Four agents that produce ranked `Pitch` objects for the Producer. Each owns its own scope, its own per-user memory, and implements the shared `DataAgent` protocol. Per master P9: real agents with decision authority, not pipeline LLM calls.

- `youtube_agent` (internal, user-selected) — YouTube subscriptions + recency signals
- `calendar_agent` (internal, user-selected) — Google Calendar events (Takeout JSON on Day 0)
- `weather_agent` (internal, user-selected) — Open-Meteo (free, no key)
- `alices_agent` (external, Producer-invoked, has `price_usdc` + `wallet_address`) — @AlicesLens

## Key premises (from master)

- **P3** Demo ships with exactly 4 agents; roadmap agents deferred
- **P7** Real pre-captured demo data (YouTube Takeout, etc.)
- **P8** Live gen first, cached fallback
- **P9** Agents are real agents; per-user memory; own `DataAgent` interface
- **P10** External agent invoked by Producer, paid agentically

## Interface contract

```python
class DataAgent(Protocol):
    name: str                                  # "youtube", "calendar", "weather", "alices"
    display_name: str                          # "@YouTube", "@AlicesLens"
    scope: str                                 # human-readable scope description
    external: bool                             # True for creator agents only
    price_usdc: float | None                   # None for internal agents
    wallet_address: str | None                 # None for internal agents

    def load_memory(user_id: str) -> AgentMemory: ...
    def fetch_context(user_id: str) -> ScopeContext: ...
        # fetch_context() does not receive Brief — weather and calendar agents
        # produce today-context data that the orchestrator assembles INTO Brief.
        # Passing Brief here would be a circular dependency. Brief is only
        # passed to pitch(). See prompt_design.md §3 for the sync barrier.
    def pitch(brief: Brief, memory: AgentMemory, context: ScopeContext,
              user_id: str) -> list[Pitch]:
        """3–5 ranked pitches, or exactly 1 thin-signal pitch when data is insufficient.
        Never any other cardinality. See prompt_design.md §4 for thin-signal shape."""
    # observe() was dropped 2026-04-15 — learning-loop consumes EpisodeSignals
    # directly from api-storage at session-end and writes signal-derived memory
    # fields itself. See agents/youtube/docs/DESIGN.md §`AgentMemory` schema.
```

**`user_id` in `pitch()`.** Required for deterministic tie-breaking in candidate selection (`top_n_seeded` uses `(user_id, profile.computed_at)` as seed). See youtube spec §`pitch()` flow.

### `Brief` shape

`Brief` is the per-episode context object assembled by the orchestrator before agents pitch. All agents receive the same `Brief`.

```python
class TodayContext(TypedDict):
    date: str                           # ISO 8601 date
    day_of_week: str                    # "Tuesday"
    time_of_day: str                    # "morning" | "afternoon" | "evening" | "night"
    weather_summary: str | None         # "rainy, 14°C" — None if weather fetch failed
    calendar_events: list[str] | None   # ["Team standup 10am", "Dentist 3pm"] — None if no calendar agent

class Brief(TypedDict):
    today_context: TodayContext         # assembled by orchestrator from weather + calendar ScopeContext
    # v1+: user_preferences, episode_format, target_duration, etc.
```

`Brief.today_context` is populated by the orchestrator after Phase 1 (`fetch_context()`). Weather and calendar agents return their data as `ScopeContext` fields; the orchestrator reads those and assembles `today_context` before calling `pitch()`. See prompt_design.md §3 for the full flow.

### `ScopeContext` shape

`ScopeContext` is agent-specific — each agent's `fetch_context()` returns a `ScopeContext` carrying the data that agent's `pitch()` needs. The base shape is a TypedDict; agents extend it with their own fields.

```python
class ScopeContext(TypedDict, total=False):
    """Base shape. Each agent adds its own fields."""
    pass

# youtube_agent's ScopeContext carries the InterestProfile:
#   profile: InterestProfile        — from memory.profile_state (write-through on fetch success)
#
# weather_agent's ScopeContext carries:
#   weather_summary: str            — assembled into Brief.today_context by orchestrator
#
# calendar_agent's ScopeContext carries:
#   calendar_events: list[str]      — assembled into Brief.today_context by orchestrator
#
# alices_agent's ScopeContext carries:
#   profile: InterestProfile        — from committed Day-0 JSON via shared extractor
```

### `Pitch` shape

Base fields (all agents):

```json
{
  "agent": "youtube",
  "title": "...",
  "hook": "...",
  "suggested_length_sec": 90,
  "rationale": "...",
  "source_refs": ["..."],
  "priority": 0.91
}
```

Extended fields (set by the algo step in each agent's `pitch()`, not by the LLM):

```json
{
  "thin_signal": false,
  "claim_kind": "rising",
  "provenance_shape": "balanced"
}
```

- **`thin_signal`** (`bool`): `true` iff agent emitted exactly 1 pitch due to insufficient data. Producer uses this for time-budget allocation only; no special user-facing language.
- **`claim_kind`** (`"durable" | "rising" | "discovery" | "neutral"`): deterministic temporal-framing constraint. See prompt_design.md §1 for preconditions and LLM prompt contract. Computed by `youtube_agent` and `alices_agent` (both use the shared extractor and `InterestProfile`). Weather and calendar agents default to `"neutral"`.
- **`provenance_shape`** (`"balanced" | "sub_only" | "like_only"`): deterministic evidence-framing constraint. See prompt_design.md §2 for per-shape LLM directives. Computed by `youtube_agent` and `alices_agent`. Weather and calendar agents default to `"balanced"`.

## Dependencies on other components

| Component       | Contract                                                                                                                                          | Direction                                         |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| `learning-loop` | `AgentMemory` shape, `EpisodeSignals` shape (both locked 2026-04-15 in `agents/youtube/docs/DESIGN.md`); learning-loop owns signal-derived writes | agents consume memory schema; no `observe()` hook |
| `producer`      | consumes `list[Pitch]`                                                                                                                            | agents emit; producer ranks                       |
| `payment`       | `alices_agent` has `wallet_address` that receives tx                                                                                            | payment reads wallet from agent                   |
| `api-storage`   | `agent_memory` table (jsonb per user_id+agent_name)                                                                                               | agents persist through api-storage                |

## Build plan touchpoints

- **Day 0:** Alice session (~30-45 min) — export his YouTube subscriptions/history, capture 5-10 topics, draft agent content in his voice. Optional: record 2-min voice sample for cloning (audio component uses this).
- **Day 1:** Scaffold `DataAgent` protocol + 3 internal agents. CLI command prints valid running-order JSON end-to-end with all 3 pitching. Tests ensure each agent returns valid `Pitch` objects — this is the contract everything else depends on.
- **Day 4:** `alices_agent` external with hand-loaded content pack from Day 0 session.

## Success criteria

- Each agent returns ≥1 valid `Pitch` on real data (no mocks)
- Per-agent memory round-trips through Supabase `agent_memory` table
- `alices_agent` has non-null `price_usdc=0.10` and `wallet_address` set to a Base Sepolia address you control
- Tests lock the `DataAgent` protocol so future agents (roadmap) can't break the contract

## Reviewer concerns

### 1. `priority` computation (A-Clarity, severity: medium) — RESOLVED 2026-04-15

**Original concern:** Master uses `priority: 0.91` but never specifies who computes it or with what formula.

**Resolution:** Each agent's `pitch()` has a two-step pipeline: (1) deterministic algo step assembles candidates and computes per-candidate scores, (2) LLM step selects 3–5 and assigns `priority ∈ [0, 1]`. For youtube_agent specifically, the algo score is `combined_topic_scores[T] * topic_multiplier.get(T, 1.0)` — the LLM sees this score in the candidate bundle and sets priority informed by it. See youtube spec §`pitch()` flow for the full mechanism.

**Producer sees only the scalar `priority`, never raw scores or memory.** This IS the memory-isolation invariant (P9) in executable form.

The original sigmoid formula (`entity_scores`, `topic_scores`, `pitch.topic_tags`) was a pre-youtube-spec placeholder. The youtube spec's `InterestProfile` replaced entity-level scoring with topic-level TF-IDF; memory replaced `entity_scores`/`topic_scores` with `topic_multiplier: dict[str, float]`. The sigmoid is superseded.

### 2. SSE `phase` field missing on agent events (A-Completeness, severity: medium)

Master's SSE schema reuses `agent.pitching.started` / `agent.pitch.emitted` / `agent.pitching.done` for both the internal pitch round and the external pitch round that fires after payment. UI can't distinguish them.

**Fix:** every `agent.pitching.*` event carries `phase: "internal" | "external"`. Coordinate with `api-storage`.

### 3. Voice cloning risk (A-Scope, severity: low)

Alice's voice-cloning on Day 0 is labeled fallback in master — if Alice declines or the sample is poor, use a stock ElevenLabs voice distinct from the narrator. No action required; already documented. Flagged here so `audio` component is aware.

## Open questions (component-scoped)

### Resolved (2026-04-15)

- **Agent memory cold-start** — resolved in youtube spec §Bootstrap defaults + §Cross-field invariants. `topic_multiplier == {} → .get(T, 1.0)` default makes cold-start identical to "all topics at neutral multiplier." No special branching needed. Empty `combined_topic_scores` triggers thin-signal pitch (exactly 1 pitch, see prompt_design.md §4).
- **`topic_tags` extraction** — resolved in youtube spec §Topic tagging. Tags come from YouTube Data API's `topicCategories` (Wikipedia URLs → kebab-case normalization). Channel-level tags via `channels.list?part=topicDetails` (server API key); per-video tags via `videos.list?part=topicDetails`. Coverage: 100% channels, 89.6% videos on dev data. No LLM fallback needed for v0. `alices_agent` uses the same tags from committed Day-0 JSON.

### Open

- **Weather/calendar agent `pitch()` prompt design.** These agents are structurally simpler (structured input → pitch, no TF-IDF) but their LLM calls still need prompt specs. Deferred to their respective design sessions. prompt_design.md specifies only their `today_context` population contract and `ScopeContext` fields.

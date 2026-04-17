# Component: `agents`

**Status:** DRAFT (component extract from master design, reconciled 2026-04-15)
**Master doc:** [`~/.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md`](../../../../.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md) — canonical source; this doc is scoped to the `agents` component only.
**Child specs:**
- [`agents/youtube/docs/DESIGN.md`](../youtube/docs/DESIGN.md) — `InterestProfile`, TF-IDF, K=5 provenance, `pitch()` flow, `AgentMemory` schema (locked 2026-04-15)
- [`agents/docs/prompt_design.md`](prompt_design.md) — hook guardrails (`claim_kind`, `provenance_shape`), today's-context handoff, Producer running-order, `EpisodeScript` output
**Reviewed:** 2026-04-13 (spec review 6/10, red-team); reconciled 2026-04-15 against child specs

## Purpose

Four agents that produce ranked `Pitch` objects for the Producer. Each owns its own scope, its own per-user memory, and implements the shared `DataAgent` protocol. Per master P9: real agents with decision authority, not pipeline LLM calls.

- `youtube_agent` (internal, user-selected) — YouTube subscriptions + recency signals (live OAuth)
- `calendar_agent` (internal, user-selected) — Google Calendar events (live OAuth)
- `weather_agent` (internal, user-selected) — Open-Meteo (free, no key; requires GPS location)
- `alices_agent` (external, Producer-invoked, has `price_usdc` + `wallet_address`) — @AlicesLens (pre-captured data)

## Agent Selection & Auth Sequence (decided 2026-04-16)

The demo opens with an agent selection screen. The user (demonstrator) picks which agents to enable. Upon selection, auth/setup flows run **sequentially** in a fixed order:

```
Agent Selection Screen
  |
  +-- User picks agents (e.g. all four: YouTube, Calendar, Weather, Alice)
  |
  +-- Sequential auth (only for selected agents that need it):
      |
      1. YouTube OAuth  — google consent page opens, user approves youtube.readonly
      |                    token stored at ~/.config/radio-podcast/youtube_token.json
      |
      2. Calendar OAuth — google consent page opens, user approves calendar.readonly
      |                    token stored at ~/.config/radio-podcast/calendar_token.json
      |
      3. Weather GPS    — browser geolocation prompt, user approves location access
      |                    lat/lon stored in user profile (in-memory for demo)
      |
      4. Alice        — no setup needed (pre-captured Day-0 data in repo)
      |
  +-- All auth complete → orchestrator runs episode generation
```

**Why sequential, not parallel:** Each auth step opens a browser popup/prompt. Parallel popups confuse users and may trigger browser popup blockers. Sequential is the honest UX — user sees each permission clearly.

**Why this order:** YouTube is the highest-value agent (most data, longest OAuth flow). Calendar is the second Google OAuth. Weather GPS is instant (browser prompt, no redirect). Alice needs nothing. Front-loading the slower OAuth flows means the user isn't waiting at the end.

**Demo moment:** The sequential auth IS the demo. Judge watches the demonstrator approve YouTube, then Calendar, then GPS — each time feeding real personal data into the system. By the time episode generation starts, every agent has live data. This is the "real agents with real data" thesis (P7, P9) made visible.

**Skipped agents:** If the user doesn't select an agent, its auth step is skipped entirely. The orchestrator only runs agents that were selected. If an auth flow fails or is denied, that agent is excluded from the episode (graceful degradation, not a hard failure).

## Key premises (from master)

- **P3** Demo ships with exactly 4 agents; roadmap agents deferred
- **P7** Real pre-captured demo data (YouTube Takeout, etc.)
- **P8** Live gen first, cached fallback
- **P9** Agents are real agents; per-user memory; own `DataAgent` interface
- **P10** External agent invoked by Producer, paid agentically

## Two-LLM boundary: why agents pitch and Producer scripts (decided 2026-04-16)

The pipeline has two LLM boundaries — one inside each agent's `pitch()`, one inside Producer's script pass. This is not an accident of layering; it's the primary mechanism for limiting hallucination surface across the system.

**Taste vs. production.** Agents know the user's taste; Producer knows how to make radio. The agent LLM operates on provenance (channels, videos, subscription dates, like timestamps) and writes hooks constrained by deterministic `claim_kind` + `provenance_shape` guardrails. The Producer LLM operates on `Pitch` objects + `Brief.today_context` and writes episode scripts with segues, cold opens, and today's framing. Neither LLM is asked to do the other's job. An agent never scripts radio; Producer never classifies taste signals.

**Hallucination surface and why separation limits it.** Each LLM call has a hallucination surface proportional to the gap between what it's asked to produce and what its input can verify. Collapsing both boundaries into a single Producer LLM call would force one model to simultaneously (a) select topics from scored candidates with provenance evidence, (b) write hooks that respect temporal and evidence constraints, and (c) script a full episode with segues and today's context. The combined input surface — provenance entries, scores, weather, calendar, episode pacing — is large enough that factual claims about user taste become unverifiable noise in a scripting context. Separation keeps each LLM's input small and its claims auditable:

- **Agent LLM** input: ~8 candidates with provenance, scores, `claim_kind`, `provenance_shape`. Output: 3–5 hooks. Every factual claim maps to a provenance entry the system can trace. Guardrails are structural (deterministic `claim_kind` computed pre-LLM), not prompt-only.
- **Producer LLM** input: 4–8 selected `Pitch` objects (hooks already written and constrained) + `today_context`. Output: episode script. Producer never sees raw provenance or scores — it can't hallucinate taste claims because it doesn't have the raw evidence to misinterpret. It inherits the agent's already-constrained hooks and wraps them in radio narration.

The boundary also means a hallucination in one layer doesn't compound in the other. If an agent hook slightly overstates interest (scoring 1/2 on the non-fabrication rubric), Producer's script doesn't amplify it further because Producer treats the hook as a creative brief, not as evidence to extrapolate from. Without the boundary, a single LLM seeing raw provenance + episode context would both misclassify the evidence *and* script confident narration around the misclassification — compounding the error in the final spoken output.

**What stays in the agent LLM vs. what moves to Producer.** Agent `pitch()` owns: topic selection (3–5 from ~8 candidates), hook writing (constrained by guardrails), and priority assignment (taste-informed weighting). Producer owns: segment selection from the pitch pool (deterministic `select_segments()`), running order, segment lengths, segues, cold open, sign-off, and today's-context weaving. Segment length is a production concern — agents have no concept of radio pacing. Producer assigns lengths from a per-agent default table (`DEFAULT_SEGMENT_SEC` in `producer/segments.py`) and can override via `length_overrides` (e.g. from Producer memory or user preferences). When marketplace agents need self-description, `default_segment_sec` moves to `DataAgent` metadata.

See prompt_design.md §1–§2 for the guardrail specifics, and youtube spec §Step 2 for the input-bounded constraint.

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
- Internal agents (`youtube`, `calendar`, `weather`) memory round-trips through Supabase `agent_memory` table; external agents are exempt (fixed pre-captured data, learning loop does not apply)
- `alices_agent` has non-null `price_usdc=0.10` and `wallet_address=0x8043AeeD92c681492B13f46e91EFb8B42D18E3b2` (Base Sepolia)
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

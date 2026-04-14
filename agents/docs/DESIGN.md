# Component: `agents`

**Status:** DRAFT (component extract from master design)
**Master doc:** [`~/.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md`](../../../../.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md) — canonical source; this doc is scoped to the `agents` component only.
**Reviewed:** 2026-04-13 (spec review 6/10, red-team)

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
    def fetch_context(brief: Brief) -> ScopeContext: ...
    def pitch(brief: Brief, memory: AgentMemory, context: ScopeContext) -> list[Pitch]:
        """3–5 ranked pitches. Each pitch has topic, hook, suggested_length_sec, rationale."""
    def observe(episode_id: str, signals: EpisodeSignals) -> None:
        """Updates memory after user reacts. Owned by the learning-loop component."""
```

**Pitch shape:**

```json
{
  "agent": "youtube",
  "title": "...",
  "hook": "...",
  "suggested_length_sec": 240,
  "rationale": "...",
  "source_refs": ["..."],
  "priority": 0.91
}
```

## Dependencies on other components

| Component | Contract | Direction |
|---|---|---|
| `learning-loop` | `AgentMemory` shape, `EpisodeSignals` shape, update rules | agents consume memory schema + observe() rules |
| `producer` | consumes `list[Pitch]` | agents emit; producer ranks |
| `payment` | `alices_agent` has `wallet_address` that receives tx | payment reads wallet from agent |
| `api-storage` | `agent_memory` table (jsonb per user_id+agent_name) | agents persist through api-storage |

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

### 1. `priority` formula ambiguity (A-Clarity, severity: medium)

Master uses `priority: 0.91` in the Pitch shape but never specifies who computes it or with what formula. Producer is memory-blind (P9 invariant), so the agent must compute `priority` before handing the Pitch over.

**Spec:** each agent computes
```
priority = sigmoid(
  base_priority(pitch)
  + w_entity * entity_scores[pitch.entity]
  + w_topic  * sum(topic_scores[t] for t in pitch.topic_tags) / len(pitch.topic_tags)
)
```
Output range `[0, 1]`. Weights are per-agent (agents may emphasize entity vs. topic differently). **Producer sees only the scalar `priority`, never raw scores.** This IS the memory-isolation invariant in executable form.

### 2. SSE `phase` field missing on agent events (A-Completeness, severity: medium)

Master's SSE schema reuses `agent.pitching.started` / `agent.pitch.emitted` / `agent.pitching.done` for both the internal pitch round and the external pitch round that fires after payment. UI can't distinguish them.

**Fix:** every `agent.pitching.*` event carries `phase: "internal" | "external"`. Coordinate with `api-storage`.

### 3. Voice cloning risk (A-Scope, severity: low)

Alice's voice-cloning on Day 0 is labeled fallback in master — if Alice declines or the sample is poor, use a stock ElevenLabs voice distinct from the narrator. No action required; already documented. Flagged here so `audio` component is aware.

## Open questions (component-scoped)

- **Agent memory cold-start:** on a user's very first generation, all `entity_scores` / `topic_scores` are empty. Does `priority` default to `base_priority(pitch)` alone? Yes. Document this.
- **`topic_tags` extraction:** where does a pitch's topic tags come from? Agent-level LLM call at pitch-time, OR pre-computed on fetch_context, OR hand-tagged in content packs for `alices_agent`? **Recommended:** agent-level at pitch-time for YouTube/Calendar/Weather; hand-tagged for `alices_agent` (content is curated upstream).

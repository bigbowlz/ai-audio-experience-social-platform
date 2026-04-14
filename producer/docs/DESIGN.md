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
7. Runs `write_script()` → Script (per-segment in P13 streaming flow)
8. Owns Producer-memory (pacing preferences; independent of domain-agent memory)

## Key premises

- **P8** Live gen first, cached fallback (with honest labeling)
- **P9** Producer is distinct component; memory-blind by design (invariant)
- **P10** External-agent invocation + agentic payment is Producer's call, not user's
- **P13** Streaming segment TTS → `write_script()` emits per-segment not monolithic

## Interface contract

```python
class Producer:
    def decide_external_invocation(pitches: list[Pitch]) -> ExternalDecision:
        """v0: always invokes. Returns {decision: 'invoke', rationale: str}.
        v1: conditional on topic-cluster entropy / cocoon detection."""

    def query_marketplace() -> list[CreatorAgentListing]:
        """v0: reads hardcoded list. Returns candidates with handle + price_usdc + wallet."""

    def select_external(candidates: list[CreatorAgentListing], brief: Brief) -> CreatorAgentListing:
        """v0: picks @AlicesLens (only listing that matches seed topics)."""

    def select(pitches_by_agent: dict[str, list[Pitch]], brief: Brief) -> RunningOrder:
        """Picks subset fitting total duration, allocates per-agent airtime, orders.
        Sees only the scalar `priority` on each Pitch (memory-isolation invariant)."""

    def write_script(order: RunningOrder, contexts: dict[str, ScopeContext]) -> AsyncIterator[SegmentScript]:
        """Emits segments one at a time. Segment 1 first for P13 streaming."""
```

**RunningOrder shape:** see master.
**ExternalDecision + CreatorAgentListing:** new shapes; document in this file when implemented.

## Dependencies on other components

| Component | Contract | Direction |
|---|---|---|
| `agents` | consumes `list[Pitch]` with `priority: float` scalar only | in |
| `payment` | calls `payment.initiate_tx(producer_wallet, agent_wallet, 0.10)` | out |
| `audio` | emits per-segment scripts for streaming TTS | out |
| `learning-loop` | reads Producer-memory; writes Producer-memory at session end | in/out |
| `api-storage` | emits SSE events for every stage | out |

## Build plan touchpoints

- **Day 1:** Stub Producer. `select()` picks top-N by priority fitting total_length_sec budget. `write_script()` emits segments as one structured-output call (monolithic). CLI prints valid RunningOrder JSON. End-to-end works.
- **Day 4:** External-invocation decision (unconditional), marketplace stub (hardcoded candidates), `select_external()` → @AlicesLens. Wire payment call between internal pitches and external pitch.
- **Day 5 (STRETCH):** Producer-memory (`opener_preference` only, see Reviewer Concern #4). Refactor `write_script()` to per-segment async iterator for P13 streaming.

## Success criteria

- Day 1: valid `RunningOrder` JSON end-to-end, coherent script
- Day 4: `decide_external_invocation()` fires, payment call triggered, external pitch factored into final running order on the same `priority` axis (no forced opening, no priority boost)
- Day 5: Episode B reorders running order in at least one visible way vs. Episode A (opener change counts)

## Reviewer concerns

### 1. `write_script()` monolithic call risks Minute 6-8 beat (severity: CRITICAL) — B-1

Master says `write_script()` is one structured-output LLM call. On Monday LLM load + one malformed-JSON retry, Episode B live regen blows the 75-sec budget. Screen sits on "writing script..." while builder narrates over silence. The whole "compounding personalization" beat dies.

**Fix (Day 5 morning, hard requirement):**
- Split `write_script()` into per-segment async iterator
- Segment 1 is critical-path under P13 (target ~3-5s LLM + ~3-5s TTS = ~6-10s to first audio)
- Segments 2-N stream in background while segment 1 plays
- Each segment is its own structured-output call with a tight JSON schema (less room for malformed retry)

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

### 4. Producer-memory scope creep (severity: medium) — A-Scope

Master's Producer-memory has 4 fields: `target_length_preference_sec`, `opener_agent_preference`, `fatigue_point_sec`, `segment_count_preference`. All updated via exponential smoothing α=0.3.

**Fix (Day 5 scope cut):** ship `opener_agent_preference` only. It's the most visible shift in Episode B (different agent opens). Defer `target_length`, `fatigue_point`, `segment_count` to v1. Reduces Day 5 cognitive load; still produces a "pacing learns" beat.

### 5. `priority` formula + memory-isolation invariant (severity: low, doc-clarity) — A-Clarity

Producer must NEVER read raw `entity_scores` / `topic_scores` from agent memory. Sees only the scalar `priority: float` on each Pitch. See `agents/docs/DESIGN.md` Reviewer Concern #1 for the formula. Enforced by code review; no programmatic check needed in v0.

## Open questions (component-scoped)

- **Tie-breaking in `select()`:** two pitches at identical `priority` — first-emitted wins? agent-index order? random-seed? **Recommended:** deterministic by `(priority DESC, agent_name ASC)` so tests are reproducible.
- **Airtime reconciliation:** if selected pitches sum to ≠ target length, does Producer truncate a pitch or add a filler music transition? **Recommended:** pitch-level `length_sec` is a request from the agent; Producer is the authority and may shrink to fit (`min(suggested_length_sec, available_budget)`).

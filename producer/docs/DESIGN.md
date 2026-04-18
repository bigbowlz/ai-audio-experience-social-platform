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
- **Day 5 (STRETCH):** Producer-memory inter-agent weights (`agent_weights` driven by like/replay/skip, see §Producer-memory learning rule). Refactor `write_script()` to per-segment async iterator for P13 streaming.

## Success criteria

- Day 1: valid `RunningOrder` JSON end-to-end, coherent script
- Day 4: `decide_external_invocation()` fires, payment call triggered, external pitch factored into final running order on the same `priority` axis (no forced opening, no priority boost)
- Day 5: Episode B reorders running order in at least one visible way vs. Episode A (e.g., a boosted agent wins a bonus slot that an unweighted run would have lost, or a demoted agent loses a bonus slot it previously held)

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

### 4. Producer-memory scope (severity: medium) — A-Scope [REVISED 2026-04-17]

**Previous decision (2026-04-13).** Ship `opener_agent_preference` only — exponential smoothing α=0.3 on a pacing preference.

**Revised decision (2026-04-17).** Pacing (opener, length, fatigue, segment-count preferences) is v1. **v0 ProducerMemory holds inter-agent weights only**, driven by user interaction signals (like / replay / skip). Rationale: pacing changes are invisible until they're wrong, and hard to attribute to any one user signal. Inter-agent weights driven by per-segment feedback form a tight, legible loop — skip calendar → calendar loses bonus-slot competitiveness next episode → visible, attributable behavior change.

**Scope boundary (invariant).** ProducerMemory holds INTER-agent weights only. Intra-agent weights (`profile_state`, `topic_multiplier`, superseded `entity_scores` / `topic_scores`) live in `AgentMemory` and stay inside the agent that owns them. Moving any intra-agent signal into ProducerMemory breaks the marketplace invariant — Producer must remain memory-blind to agent-level state (P9).

**Deterministic application invariant.** ProducerMemory is applied via a pure function BEFORE selection, never passed as free-form input into any LLM prompt. See §Producer-memory learning rule (v0) below.

### 5. `priority` formula + memory-isolation invariant (severity: low, doc-clarity) — A-Clarity

Producer must NEVER read raw agent memory fields (`profile_state`, `topic_multiplier`, or the superseded `entity_scores` / `topic_scores`). Sees only the scalar `priority: float` on each Pitch. See `agents/docs/DESIGN.md` Reviewer Concern #1 for the formula. Enforced by code review; no programmatic check needed in v0.

## Producer-memory learning rule (v0)

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

| Signal   | Source                            | Rule (per event, pre-clamp)          |
|----------|-----------------------------------|--------------------------------------|
| `like`   | listener taps like on a segment   | `w[agent] *= 1.10`                   |
| `replay` | listener replays a segment        | `w[agent] *= 1.20`                   |
| `skip`   | listener skips a segment          | `w[agent] *= 0.90`                   |

After each event, clamp to `[AGENT_WEIGHT_MIN, AGENT_WEIGHT_MAX]`. Multiplicative updates are commutative within a session, so order of feedback events within one episode does not affect the final weight. Writes are idempotent per `(episode_id, segment_index, signal)` — learning-loop dedupes before applying. Constants (1.10, 1.20, 0.90) are chosen so ~10 consecutive likes saturate the upper clamp and ~10 skips saturate the lower clamp — roughly 2–3 episodes to register a strong preference.

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

| Fixture | Setup | Expected |
|---------|-------|----------|
| **Default weights** | `agent_weights = {}` | All priorities unchanged. |
| **Single agent boosted** | `agent_weights = {"youtube": 1.5}` | youtube pitches scaled 1.5×; others unchanged. |
| **Intra-agent order preserved** | youtube weight 1.5; priorities `[0.9, 0.7, 0.5]` | Post-adjust `[1.35, 1.05, 0.75]`; relative order unchanged, argmax unchanged. |
| **Cross-agent bonus reorder** | youtube weight 1.5, alices weight 1.0; pre: alices pitch 0.7 > youtube pitch 0.5 | Post: youtube 0.75 > alices 0.7 — youtube wins bonus sort. |
| **Weight clamped (over MAX)** | `agent_weights = {"youtube": 5.0}` | Effective weight = 2.0; priorities scaled 2.0×. |
| **Weight clamped (under MIN)** | `agent_weights = {"calendar": 0.01}` | Effective weight = 0.3. |
| **Weight malformed (negative, NaN, None)** | `agent_weights = {"youtube": -0.5}`, `{"weather": float("nan")}`, `{"calendar": None}` | Clamped to `[MIN, MAX]`; NaN/None treated as default 1.0 (no propagation). |
| **Demoted agent still guaranteed** | `agent_weights = {"calendar": 0.3}`, calendar has 1 pitch priority 0.5 | Pipeline (`apply_producer_memory` → `select_guaranteed_slots`): calendar still appears in guaranteed; its pitch priority = 0.15. |
| **Memory absent** | `memory = {}` or missing `agent_weights` key | All priorities unchanged — `.get("agent_weights", {})` defaults cleanly. |
| **Bootstrap identity** | `memory = bootstrap_producer_memory()` | All priorities unchanged; function returns a dict shape-equal to input under priority comparison. |

End-to-end pipeline (the product-visible claim):

| Fixture | Setup | Expected |
|---------|-------|----------|
| **Bonus-slot flip** | 4 agents with 3 pitches each; pre-adjust, alices' #2 pitch would win the last bonus slot over youtube's #2 pitch by priority. With `DISABLE_LLM=1` to force the deterministic fallback, set `agent_weights = {"youtube": 1.5}`. | After `apply_producer_memory → select_guaranteed_slots → select_bonus_segments_llm` (fallback path), the final running order contains youtube's #2 pitch in the bonus slot, not alices' #2. Guaranteed slots are unchanged. |

Writer — learning-loop (fixtures live in learning-loop test suite; design-locked here):

| Fixture | Setup | Expected |
|---------|-------|----------|
| **Saturation boundary** | Start at `w = 1.0`; apply `like` × 10 (no decay). | Final `w = 2.0` (clamped on or before the 10th event: `1.10^8 ≈ 2.14` clamps on the 8th event; test asserts clamp is the final value, not the pre-clamp multiplication). |
| **Idempotency** | Apply `(episode_id=E1, segment_index=2, signal=like)` twice. | Weight updated once; second application is a no-op (dedupe). |
| **Per-episode EMA decay** | `agent_weights = {"youtube": 2.0, "calendar": 0.3}`; run `decay_agent_weights()` once with no feedback. | youtube: `0.95·2.0 + 0.05·1.0 = 1.95`; calendar: `0.95·0.3 + 0.05·1.0 = 0.335`. Both move one step toward 1.0. |

Integration — SSE:

| Fixture | Setup | Expected |
|---------|-------|----------|
| **`producer.memory.applied` emission** | `agent_weights = {"youtube": 1.5, "calendar": 0.8}`; pitches with known raw priorities. | One `producer.memory.applied` event fires before `producer.selecting.started`, payload contains `{agent_weights, changes: [{agent, pre_max_priority, post_max_priority}, ...]}`. No event emitted when `agent_weights == {}` (silent identity). |

### Non-goals (v0)

| Non-goal | Why |
|---|---|
| Pacing preferences (opener, length, fatigue, segment_count) | v1. Moved out of v0 — see Reviewer Concern #4. Not writable from likes/replays/skips without a separate signal layer. |
| Topic-level or content-level weights | Violates scope boundary. See `AgentMemory.topic_multiplier`. |
| Cross-user / aggregate priors | v1+. v0 memory is per-user. |
| LLM-based weight inference / raw memory in prompts | The pure function is the whole point — behavior must be testable and honest about where the decision happens. |

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

## Open questions (component-scoped)

- **Tie-breaking in `select()`:** two pitches at identical `priority` — first-emitted wins? agent-index order? random-seed? **Recommended:** deterministic by `(priority DESC, agent_name ASC)` so tests are reproducible.
- **Airtime reconciliation:** if selected pitches sum to ≠ target length, does Producer truncate a pitch or add a filler music transition? **Resolved (2026-04-16):** Producer owns segment lengths via `DEFAULT_SEGMENT_SEC` lookup in `producer/segments.py`. Agents do not set `suggested_length_sec`. `select_segments()` accepts `length_overrides` so Producer memory or user preferences can adjust per-agent defaults. All lengths are clamped to `MAX_SEGMENT_SEC`.

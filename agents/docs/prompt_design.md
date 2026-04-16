# Prompting Pipeline Design

**Status:** DRAFT (2026-04-15)
**Parent:** [`agents/docs/DESIGN.md`](DESIGN.md) — `DataAgent` protocol, `Pitch` shape, `Brief` shape
**YouTube agent spec:** [`agents/youtube/docs/DESIGN.md`](../youtube/docs/DESIGN.md) — `InterestProfile`, TF-IDF, K=5 provenance, `pitch()` flow
**Scope:** Two LLM boundaries — (1) each domain agent's `pitch()` call, (2) the Producer's episode-script call. Covers hook guardrails, provenance-shape handling, today's-context handoff, and running-order assembly.

**Out of scope:** InterestProfile internals (locked in youtube spec), learning-loop update rules (separate session), weather/calendar agent internals (undesigned — this doc specifies only their interface with the prompting layer).

## Pipeline overview

```
  AGENTS (per-agent, parallel)                    PRODUCER (single, sequential)
  ┌─────────────────────────────────┐             ┌─────────────────────────────────────┐
  │ 1. fetch_context() → data       │             │ 1. Deterministic prelude            │
  │ 2. Algo: score, rank, top-8,    │             │    - 1 slot per agent (guaranteed)  │
  │    compute claim_kind per topic │             │    - bonus slots by priority        │
  │ 3. LLM: write hooks constrained │  pitches    │    - time-budget gate               │
  │    by claim_kind + provenance   │ ──────────► │ 2. LLM: write full episode script   │
  │ 4. Emit 3–5 Pitch objects       │             │    - cold open, segments, segue,    │
  │    (or 1 thin-signal Pitch)     │             │      sign-off                       │
  └─────────────────────────────────┘             │    - today's context woven in       │
                                                  └─────────────────────────────────────┘
```

**Boundary contract.** Agents produce `Pitch` objects — informational briefs with hooks as structured creative input. Producer owns all scripting. Agent hooks are never spoken verbatim; they are input to Producer's script-writing LLM. Agents know the user's taste; Producer knows how to make radio. For why this two-LLM boundary exists (taste/production separation, hallucination surface isolation), see [`agents/docs/DESIGN.md` §Two-LLM boundary](DESIGN.md#two-llm-boundary-why-agents-pitch-and-producer-scripts-decided-2026-04-16).

---

## §1 Hook hallucination guardrail (decided 2026-04-15)

### Failure mode

The youtube_agent's `pitch()` LLM receives a topic + scores + K=5 provenance and writes a hook like "you've been deep into jazz lately." With thin provenance (e.g., 1 like from 6 months ago on a rock channel that happened to be tagged `jazz`) the LLM can write confident wrong claims. One bad sentence breaks user trust. The failure is not model quality — it's that the LLM is asked to both _classify the evidence shape_ and _write the hook_, and gets the classification wrong silently.

### Solution: deterministic `claim_kind` (decided 2026-04-15)

Split classification from articulation. The algo step computes `claim_kind` per candidate topic as a pure function of provenance + scores. The LLM receives `claim_kind` as a constraint and writes hooks that conform to its permitted phrasing. The LLM never chooses claim_kind.

```python
class ClaimKind(str, Enum):
    DURABLE  = "durable"     # longstanding interest
    RISING   = "rising"      # growing recent attention
    DISCOVERY = "discovery"  # recent-only, no established base
    NEUTRAL  = "neutral"     # fallback — state facts, no temporal claims
```

### Preconditions (deterministic, computed per candidate topic T)

| `claim_kind` | Precondition                                                                          | Rationale                                                                                                                                                                                                                                                                                                                  |
| ------------ | ------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `rising`     | `long_term[T] > 0` AND `recent[T] > long_term[T]` AND `like_count(provenance[T]) ≥ 3` AND `stats.total_recent_weight ≥ 2.0` | Topic has an established base (`long_term > 0`) but recent attention is outpacing it, with ≥3 likes as evidence floor and sufficient recent-window confidence (`total_recent_weight ≥ 2.0` prevents "rising" claims from sparse recent windows where L1-normalized shares are artificially inflated). `long_term > 0` prevents like-only topics from matching — those are `discovery`, not `rising`. Both scores are L1-normalized, so the comparison is unit-consistent (see youtube spec §Aggregation). |
| `discovery`  | `long_term[T] == 0` AND `like_count(provenance[T]) ≥ 2`                               | Topic appears only in likes, no subscriptions. ≥2 likes avoids "discovery" claims from a single drive-by like.                                                                                                                                                                                                             |
| `durable`    | `long_term[T] > 0` AND `sub_count(provenance[T]) ≥ 2`                                 | ≥2 subscriptions is the floor for "you've been into X." One sub is anecdotal.                                                                                                                                                                                                                                              |
| `neutral`    | default — none of the above hold                                                      | Safe fallback. Hook states topic + provenance facts without temporal framing.                                                                                                                                                                                                                                              |

**Evaluation order:** `rising` → `discovery` → `durable` → `neutral`. First match wins. `rising` is checked first because it's the most specific claim and has the strictest preconditions (requires both windows + count floor).

```python
def compute_claim_kind(
    topic: str,
    long_term: dict[str, float],
    recent: dict[str, float],
    provenance: list[Contributor],
    total_recent_weight: float,
) -> ClaimKind:
    sub_count = sum(1 for c in provenance if c["kind"] == "sub")
    like_count = sum(1 for c in provenance if c["kind"] == "like")
    lt = long_term.get(topic, 0.0)
    rt = recent.get(topic, 0.0)

    if lt > 0 and rt > lt and like_count >= 3 and total_recent_weight >= 2.0:
        return ClaimKind.RISING
    if lt == 0.0 and like_count >= 2:
        return ClaimKind.DISCOVERY
    if lt > 0 and sub_count >= 2:
        return ClaimKind.DURABLE
    return ClaimKind.NEUTRAL
```

### LLM prompt contract

Each candidate in the bundle passed to `pitch()`'s LLM step includes `claim_kind` alongside `topic`, `score`, `long_term`, `recent`, `provenance`. The system prompt specifies permitted phrasing per claim_kind:

| `claim_kind` | Permitted phrasing                                                                                  | Prohibited phrasing                                                                                                 |
| ------------ | --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `durable`    | "you've been into X", "a longtime favorite", reference `subscribed_at` dates                        | "lately", "recently", "getting into"                                                                                |
| `rising`     | "you've been getting into X lately", "X is taking over your feed"                                   | "longtime", "always been" (unless also `durable`-eligible, which it isn't — `rising` requires `recent > long_term`) |
| `discovery`  | "you've been exploring X", "some X caught your eye recently"                                        | "deep into", "longtime", "always"                                                                                   |
| `neutral`    | factual: "X showed up in your [subs/likes]", reference specific channel/video names from provenance | any temporal or intensity claim                                                                                     |

**Why this catches the failure mode.** The jazz-from-one-old-like scenario: `like_count = 1`, `recent[jazz]` likely small, `long_term[jazz]` likely 0 → fails `rising` (count < 3 and `total_recent_weight` likely < 2.0), fails `discovery` (count < 2), fails `durable` (lt = 0) → `neutral`. Hook can only say "jazz showed up in a recent like from [channel]" — factually correct, no confidence inflation. The `total_recent_weight` floor additionally prevents "rising" claims from sparse recent windows where a few old likes dominate the L1-normalized distribution — 3 likes from 2 years ago may pass `like_count ≥ 3` but will have `total_recent_weight ≈ 0.2`, well below the 2.0 floor.

### Rejected alternatives

| Alternative                                                                 | Why rejected                                                                                                                                                                                                                                                            |
| --------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Self-rubric LLM pass (generate hook → second LLM grades it → retry on fail) | Adds a call per pitch. Factual-drift compounds across two LLM calls. Cost scales with pitch count. The classification problem is deterministic — an LLM grading another LLM's temporal claims is strictly worse than not generating the wrong claim in the first place. |
| Pure prompt-side rules with no structural enforcement                       | LLMs violate stated preconditions silently. "Do not say 'lately' unless recent > long_term" is a soft constraint the model will break on edge cases. Deterministic computation eliminates the failure class entirely.                                                   |
| Post-hoc validator that parses LLM output and downgrades                    | If we can express the precondition deterministically (we can), compute it before the LLM call, not after. Pre-computation is strictly cheaper and prevents the bad output from being generated at all.                                                                  |

---

## §2 Asymmetric provenance shape (decided 2026-04-15)

### Problem

A topic's K=5 provenance may be **sub-only** (5 jazz subs, 0 jazz likes), **like-only** (0 anime subs, 5 anime likes), or **balanced** (mix of both). The youtube spec's "durable then fresh" narrative frame (subs first, likes second) breaks on asymmetric shapes — a sub-only topic has no "fresh" voice, a like-only topic has no "durable" voice. The spec notes this case is "part of [the K=5] revisit" and calls for a `provenance_shape` branch (see youtube spec §Provenance "K is tunable").

### Solution: `provenance_shape` field + unified template with per-shape directives

`provenance_shape` is computed at profile-build time from the `topic_provenance[T]` contributor list and stored alongside `claim_kind` in the candidate bundle.

```python
class ProvenanceShape(str, Enum):
    BALANCED  = "balanced"    # has both sub and like contributors
    SUB_ONLY  = "sub_only"    # all contributors are subs
    LIKE_ONLY = "like_only"   # all contributors are likes

def compute_provenance_shape(provenance: list[Contributor]) -> ProvenanceShape:
    has_sub = any(c["kind"] == "sub" for c in provenance)
    has_like = any(c["kind"] == "like" for c in provenance)
    if has_sub and has_like:
        return ProvenanceShape.BALANCED
    if has_sub:
        return ProvenanceShape.SUB_ONLY
    return ProvenanceShape.LIKE_ONLY
```

**Single template, not three.** The LLM prompt includes a per-shape directive block:

| `provenance_shape` | Directive to LLM                                                                                                                                              |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `balanced`         | "This topic has both subscription and recent-like evidence. You may reference both durable interest (subscription dates) and recent activity (liked videos)." |
| `sub_only`         | "This topic appears only in subscriptions. Frame as established interest. Do not claim recent activity or trending behavior."                                 |
| `like_only`        | "This topic appears only in recent likes. Frame as discovery or exploration. Do not claim longstanding interest or deep familiarity."                         |

**Interaction with `claim_kind`.** `provenance_shape` and `claim_kind` are complementary constraints — `claim_kind` governs temporal framing (what the hook _claims_), `provenance_shape` governs evidence framing (what the hook _references_). Both are passed per candidate. They are consistent by construction:

- `sub_only` shape → `claim_kind` is `durable` or `neutral` (can't be `rising` or `discovery` — those require likes).
- `like_only` shape → `claim_kind` is `discovery` or `neutral` (can't be `durable` — that requires subs; can't be `rising` — that requires `long_term[T] > 0`, which is 0 for like-only topics by definition).
- `balanced` shape → any `claim_kind` is possible.

No conflict resolution needed — the precondition tables are compatible by construction.

### Hook-fidelity rubric (post-v0 measurement framework, not a v0 gate)

The youtube spec defers the K=5 and 2/3-split revisit to when "hook-hallucination measurements" exist. This rubric defines those measurements. **v0: K=5 and the 2/3 split are hardcoded. The rubric is not a gate for shipping.** It exists as the measurement framework for the post-v0 revisit and as a "what good looks like" reference for the developer eyeballing hooks during Day 3. **v0 evaluation method:** human eval by developer on 20+ hooks generated from dev-account data. v1+: LLM-assisted eval as an option. Score each hook on 5 axes, 0–2 each (10 points total):

| Axis                         | 0                                                                                                                           | 1                                                                                                   | 2                                                                                                                                   |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| **Claim-evidence grounding** | Hook makes a factual claim with no matching provenance entry                                                                | Claim is directionally supported but vague ("you like music" when provenance shows specific genres) | Every factual claim maps to a specific provenance entry (channel name, video title, date)                                           |
| **Temporal accuracy**        | Hook uses temporal language contradicted by data (says "lately" for 3-year-old sub, says "always" for 1 recent like)        | Temporal language is not contradicted but not well-supported (neutral phrasing on a strong signal)  | Temporal framing matches `claim_kind` precisely — `rising` hooks reference recent likes, `durable` hooks reference subscription age |
| **Shape alignment**          | Hook references evidence kind that doesn't exist in provenance (mentions "your subscription" for a like-only topic)         | Hook avoids contradicting shape but doesn't leverage available evidence                             | Hook's framing matches `provenance_shape` — sub-only hooks reference channels/dates, like-only hooks reference recent videos        |
| **Specificity**              | Hook is purely generic ("you like this topic")                                                                              | Hook mentions topic + one concrete detail (a channel name OR a date)                                | Hook references ≥2 concrete provenance details (channel + video title, channel + subscribe date, etc.)                              |
| **Non-fabrication**          | Hook contains a claim not derivable from any input (hallucinated channel name, invented statistic, unsupported superlative) | All claims are derivable but one is stretched (e.g., "obsessed with" from 2 likes)                  | No unsupported adjectives or claims; every characterization matches signal strength                                                 |

**Passing threshold:** ≥ 7/10 per hook. Hooks scoring < 7 indicate the LLM is writing beyond its evidence — trigger review of the prompt template, not the provenance.

**When to run.** Score a batch of hooks (≥20) on real dev-account data before finalizing K and the 2/3 split. If `specificity` scores cluster at 0–1, K may be too low (not enough evidence to reference). If `non-fabrication` scores cluster at 0–1, K may be too high (LLM is overwhelmed and hallucinating from noise). If `shape_alignment` scores are low on asymmetric cases, the per-shape directives need iteration.

### Rejected alternatives

| Alternative                                     | Why rejected                                                                                                                                                                                                                                          |
| ----------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Three separate prompt templates (one per shape) | Divergence risk — three files to keep in sync. The directives differ by one paragraph; a template branch is overkill. Unified template with a conditional directive block achieves the same outcome with one file.                                    |
| Drop provenance_shape, rely on claim_kind alone | `claim_kind` governs what you _claim_; `provenance_shape` governs what you _reference_. A `neutral` claim_kind on a `sub_only` topic should still reference subscription evidence. Collapsing both into `claim_kind` loses the evidence-framing axis. |
| LLM infers shape from provenance list           | Same problem as §1 — the LLM infers silently and may get it wrong. Shape is trivially computable from `Contributor.kind` values. No reason to delegate a deterministic classification.                                                                |

---

## §3 Today's-radio currency (decided 2026-04-15)

### Problem

youtube_agent has "no external search in `pitch()`" — content discovery is Producer's job. But episodes need today's framing ("it's a rainy Tuesday morning, perfect for some deep electronic"). Where does today's context enter the pipeline, and who writes it into the final script?

### Decision: `Brief.today_context` + Producer-owned scripting

**`Brief.today_context`** is a structured field on `Brief`, populated before agents pitch, carrying the date/time/weather/calendar context that any agent can read. For the canonical `TodayContext` and `Brief` shapes, see [`agents/docs/DESIGN.md`](DESIGN.md) §Brief shape.

**Flow:**

1. **Brief assembly** (pre-pitch). System assembles `Brief` with `today_context` from weather API + calendar API. This happens once per episode, before any agent pitches.
2. **Agent `pitch()`**. Each agent receives `Brief` including `today_context`. Agents _may_ read it for context-aware pitching (e.g., calendar_agent adjusts pitch salience based on how many events today; weather_agent's pitch is inherently today-indexed). youtube_agent's hooks do _not_ reference today's context — youtube knows taste, not the world.
3. **Producer scripting**. Producer's LLM receives all selected pitches + `Brief.today_context`. Producer writes the full episode script — cold open, per-segment scripts, segues, sign-off — weaving today's context into its own voice. "It's a rainy Tuesday, and your jazz channels have been busy" is Producer narration, not an agent claim.

**Content discovery (new albums, trending videos, world news) is Producer's responsibility.** If Producer has access to a "what's new" feed (v1+), it injects this into its script around relevant agent segments. This never flows backward into agent pitches — agents own taste signal, Producer owns world signal. v0 does not build a "what's new" feed; Producer scripts from `today_context` + pitch content only.

### Agent-specific behavior with `today_context`

| Agent            | Reads `today_context`?                | How                                                                                                                                                                                                                                                                 |
| ---------------- | ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `weather_agent`  | Yes — is the source                   | weather_agent's pitch IS today's weather. Its `fetch_context()` returns `weather_summary` in `ScopeContext`; the orchestrator assembles this into `Brief.today_context` before pitch-time.                                                                          |
| `calendar_agent` | Yes — is the source                   | calendar_agent's pitch IS today's schedule. Its `fetch_context()` returns `calendar_events` in `ScopeContext`; the orchestrator assembles this into `Brief.today_context`. Pitches event-driven topics ("you have a presentation today — here's some focus music"). |
| `youtube_agent`  | Reads but does not reference in hooks | youtube_agent's hooks are about user taste. `today_context` is available if future versions want time-of-day pitch weighting (e.g., chill topics for `night`), but v0 hooks ignore it.                                                                              |
| `alices_agent` | No                                    | Alice's content is pre-curated. Today's context is irrelevant to his segment.                                                                                                                                                                                     |

### Why agents supply `today_context` rather than Producer fetching directly

Weather and calendar agents already call their respective APIs in `fetch_context()`. Having Producer call the same APIs would duplicate the fetch. Instead, the orchestrator assembles `Brief.today_context` from agent `fetch_context()` return values — agents return context fragments, not mutate `Brief`.

**Protocol note (revised 2026-04-16).** `fetch_context(user_id: str) -> ScopeContext` per the `DataAgent` protocol takes only `user_id` and returns `ScopeContext`. `fetch_context()` does not receive `Brief` — weather and calendar agents _produce_ today-context data, so passing them a Brief with an incomplete `today_context` would be a circular dependency. `Brief` is assembled by the orchestrator _after_ Phase 1 and passed only to `pitch()`. Weather and calendar agents return their today-context data as part of their `ScopeContext` (e.g., `ScopeContext.weather_summary`, `ScopeContext.calendar_events`). The **orchestrator** reads these fields from the returned `ScopeContext` and assembles `Brief.today_context` before calling `pitch()`.

**Sync barrier (decided 2026-04-16).** The orchestrator waits for ALL `fetch_context()` calls to complete before assembling `Brief` and calling `pitch()`. This is a full sync barrier: even if weather + calendar finish early, the orchestrator waits for youtube and alices (whose `ScopeContext` is needed by their own `pitch()`). The youtube spec's 15s `fetch_context()` deadline is within the demo's 75s wall-clock budget. Staggered pitching (each agent pitches as soon as its own fetch completes) saves ~5-10s in the worst case but adds orchestration complexity not worth the 6-day build.

```
  Phase 1: fetch_context(user_id)                   Phase 2: pitch(brief, ...)
  ┌────────────────────────┐                        ┌──────────────────┐
  │ weather_agent.fetch()  │──► ScopeContext ──┐    │ all 4 agents     │
  │ calendar_agent.fetch() │──► ScopeContext ──┤    │ pitch(brief, ..) │──► Pitches
  │ youtube_agent.fetch()  │──► ScopeContext    │    │                  │
  │ alices_agent.fetch() │──► ScopeContext    │    └──────────────────┘
  └────────────────────────┘                   │           ▲
         ║                                     │           │
         ║ SYNC BARRIER: wait for ALL          │           │
         ║                                     │           │
                              Orchestrator: ───┘── Brief ──┘
                              assemble today_context
                              from weather + calendar
                              ScopeContext fields
```

All `fetch_context()` calls run in parallel. The orchestrator waits for all to complete, reads weather + calendar results, assembles `Brief.today_context`, then calls `pitch()` on all agents with the completed Brief. youtube and alices `fetch_context()` don't contribute to `today_context`.

### Rejected alternatives

| Alternative                                                                 | Why rejected                                                                                                                                                                                                                                                                                                    |
| --------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Producer rewrites agent hooks with today's context post-hoc                 | Breaks hook-ownership invariant. Agent hooks are factual claims about user taste — rewriting them with "today" framing re-opens the hallucination surface (Producer might add "new album drop" to a hook about a genre the user only casually liked). Producer writes its _own_ narration with today's framing. |
| Producer injects `fresh_evidence` field per pitch                           | Couples Producer's world-knowledge to agent output schema. Agents shouldn't receive post-pitch mutations to their Pitch objects — Pitch is agent-owned, immutable after emission.                                                                                                                               |
| youtube_agent does its own "what's new" search                              | Violates the "no external search in `pitch()`" rule. Content discovery is Producer's scope, not agent scope.                                                                                                                                                                                                    |
| No today's context in agent pitch — Producer alone fetches weather/calendar | Duplicates API calls. Weather and calendar agents already fetch this data for their own pitches.                                                                                                                                                                                                                |

---

## §4 Producer's running-order logic (decided 2026-04-15)

### Constraints

- **v0 target:** 6-minute episode. v1: variable (use case: commute, bedtime).
- **Per-agent guarantee:** each agent gets ≥1 segment. Users signal interest in an agent by selecting it before generation — giving an agent zero airtime contradicts their selection.
- **Input:** 3–5 `Pitch` objects per agent (or 1 thin-signal Pitch), total 4–21 pitches across 4 agents.
- **Output:** full episode script (cold open + ordered segments with per-segment scripts + segues + sign-off).

### Pitch shape extension

Two fields added to `Pitch` for Producer consumption (set by the algo step in each agent's `pitch()`, not by the LLM):

```python
# Added to existing Pitch shape (see agents/docs/DESIGN.md)
{
    "thin_signal": bool,          # True iff agent emitted exactly 1 pitch due to
                                  #   insufficient data. No special user-facing language;
                                  #   Producer uses this for time-budget allocation only.
    "claim_kind": str,            # "durable" | "rising" | "discovery" | "neutral"
                                  #   (see §1). Producer can use this to calibrate
                                  #   script tone for the segment.
    "provenance_shape": str,      # "balanced" | "sub_only" | "like_only"
                                  #   (see §2). Informational for Producer.
}
```

`claim_kind` and `provenance_shape` are computed by agents that use the shared YouTube extractor (`youtube_agent` and `alices_agent`) via `compute_claim_kind()` and `compute_provenance_shape()`. `alices_agent` uses the same `InterestProfile` and provenance as `youtube_agent`, so it benefits from the same hallucination guardrails. Weather and calendar agents set `claim_kind = "neutral"` and `provenance_shape = "balanced"` as defaults (their evidence shapes are structurally different and don't need the same guardrails). When those agents get their own designs, they may define agent-specific fields using the same Pitch extension pattern.

### Step 1: deterministic prelude (segment selection + time budget)

```python
TARGET_EPISODE_SECS = 360          # 6 min for v0; v1 reads from Brief or user pref
SEGUE_OVERHEAD_SECS = 10           # per inter-segment segue
OPEN_CLOSE_SECS = 25               # cold open (15s, includes transition into segment 1) + sign-off (10s)
MAX_SEGMENT_SEC = 90               # cap per segment; prevents budget overflow from guaranteed slots

# Producer owns segment lengths — agents don't set them.
# Per-agent defaults; overridable via length_overrides (from Producer memory, user prefs).
DEFAULT_SEGMENT_SEC = {"youtube": 90, "weather": 45, "calendar": 30, "alices": 90}

def select_segments(
    pitches_by_agent: dict[str, list[Pitch]],
    length_overrides: dict[str, int] | None = None,
) -> list[Pitch]:
    selected = []
    remaining = {}

    def _seg_len(pitch: Pitch) -> int:
        agent = pitch["agent"]
        if length_overrides and agent in length_overrides:
            raw = length_overrides[agent]
        else:
            raw = DEFAULT_SEGMENT_SEC.get(agent, 60)
        return min(raw, MAX_SEGMENT_SEC)

    # Phase 1: guaranteed slot — one per agent (highest priority)
    for agent, pitches in pitches_by_agent.items():
        best = max(pitches, key=lambda p: p["priority"])
        best = {**best, "suggested_length_sec": _seg_len(best)}
        selected.append(best)
        remaining[agent] = [p for p in pitches if p is not best]

    # Phase 2: bonus slots — highest priority across all remaining pitches
    budget = TARGET_EPISODE_SECS - OPEN_CLOSE_SECS
    budget -= sum(p["suggested_length_sec"] for p in selected)
    # N segments need N-1 segues: the first segment has no segue_in (cold_open
    # includes the transition into segment 1), so only inter-segment transitions
    # are counted. Each bonus segment's cost includes its own segue.
    budget -= SEGUE_OVERHEAD_SECS * (len(selected) - 1)

    all_remaining = sorted(
        [p for ps in remaining.values() for p in ps],
        key=lambda p: p["priority"],
        reverse=True,
    )

    for pitch in all_remaining:
        seg_len = _seg_len(pitch)
        cost = seg_len + SEGUE_OVERHEAD_SECS
        if budget >= cost:
            selected.append({**pitch, "suggested_length_sec": seg_len})
            budget -= cost

    return selected
```

**Segment length ownership (decided 2026-04-16).** Agents do not set `suggested_length_sec`. Producer assigns lengths from `DEFAULT_SEGMENT_SEC` (a per-agent lookup) and can override via `length_overrides` (e.g. from Producer memory learning per-agent length biases, or from user preferences). The `suggested_length_sec` field on selected pitches is set by Producer during `select_segments()`, not by agents during `pitch()`. When marketplace agents arrive, the default moves to `DataAgent` metadata so unknown agents can self-describe.

**Budget arithmetic for v0:** 360s total − 25s open/close = 335s. 4 guaranteed segments capped at 90s each = 360s max + 3 segues × 10s = 30s → 335 − 360 − 30 = −55s worst case without the cap. With `MAX_SEGMENT_SEC = 90`, guaranteed slots are bounded at 360s + 30s segues = 390s, which still exceeds the 335s budget. Default lengths (youtube=90, weather=45, calendar=30, alices=90) total 255s + 3 segues = 285s, well within the 335s budget, leaving 50s for a bonus slot. The Producer's LLM receives `target_total_secs` and adjusts segment scripts to fit. Typical v0 episode: 4 segments, possibly 5 if segments are short. The per-agent guarantee consumes most of the budget by design — it's a 6-minute show.

**Cold open → segment 1 transition.** `cold_open` includes the lead-in to segment 1. There is no separate segue between them. Segment 1's `segue_in` is empty. This is reflected in the `OPEN_CLOSE_SECS` budget (15s for the cold open that transitions into segment 1, 10s for the sign-off) and in the segue count (`len(selected) - 1` inter-segment segues, not including a cold-open-to-segment-1 segue).

**Thin-signal handling.** A thin-signal pitch has `thin_signal: true`. It competes normally in Phase 1 (it's the only pitch from that agent, so it wins its guaranteed slot). No special user-facing language. Producer's LLM scripts it like any other segment. The learning-loop receives likes/skips/replays on the segment — that's sufficient signal without prompting the user.

**Thin-signal Pitch shape.** When an agent emits a thin-signal pitch (e.g., youtube with empty `combined_topic_scores`), the Pitch fields are:

```python
{
    "agent": "youtube",
    "title": "Your YouTube world",               # generic — no topic to name
    "hook": "Not enough signal yet to personalize. Pitch a general-interest
             segment in the agent's domain.",     # creative brief to Producer
    "rationale": "Thin signal — insufficient YouTube data to rank topics.",
    "source_refs": [],                            # no provenance to cite
    "priority": 0.3,                              # low but nonzero — guaranteed slot ensures inclusion
    "thin_signal": true,
    "claim_kind": "neutral",
    "provenance_shape": "balanced",               # default; irrelevant for thin-signal
}
# Producer assigns suggested_length_sec from DEFAULT_SEGMENT_SEC["youtube"] = 90
# during select_segments(). Agent does not set it.
```

Producer scripts this segment using its own judgment for the agent's domain. The low priority means thin-signal segments will not win bonus slots, only their guaranteed slot.

### Step 2: LLM pass — episode script generation

Producer's LLM receives:

**Input:**

```python
{
    "selected_segments": [          # from Step 1, ordered by priority (LLM re-orders)
        {
            "agent": "youtube",
            "title": "Jazz exploration",
            "hook": "...",                  # agent's creative brief — not spoken verbatim
            "suggested_length_sec": 90,     # assigned by Producer from DEFAULT_SEGMENT_SEC
            "rationale": "...",
            "source_refs": [...],
            "priority": 0.91,
            "claim_kind": "rising",
            "provenance_shape": "balanced",
            "thin_signal": false,
        },
        # ... one per selected segment
    ],
    "today_context": {              # from Brief
        "date": "2026-04-15",
        "day_of_week": "Tuesday",
        "time_of_day": "morning",
        "weather_summary": "rainy, 14°C",
        "calendar_events": ["Team standup 10am", "Dentist 3pm"],
    },
    "target_total_secs": 360,
    "producer_memory": { ... },     # stub — cross-episode ordering priors, learning-loop session
}
```

**System prompt constraints:**

1. **Cannot drop segments.** Every segment in `selected_segments` must appear in the output script. Producer re-orders but does not filter.
2. **Cannot invent segments.** No topic or content not present in the input.
3. **Must produce a complete script** with: cold open (10–15s spoken), per-segment script (incorporating agent hook as creative input, not verbatim copy), inter-segment segues (5–10s each), sign-off (10s).
4. **Today's context** should be woven into cold open and segues where natural. Do not force-fit weather into every segue — use it where it adds texture.
5. **Segment ordering heuristics** (guidance, not hard rules): time-sensitive content first (calendar, weather), taste content after (youtube, alices). Within taste segments, `rising`/`discovery` claim_kinds are more narratively interesting as middle-of-show energy; `durable` works as a comfortable closer.
6. **Respect `claim_kind` and `provenance_shape`** per segment — do not add temporal claims the agent's hook didn't make. If `claim_kind` is `neutral`, the segment script should be factual, not enthusiastic.

**Output schema:**

```python
class EpisodeScript(TypedDict):
    cold_open: str                          # spoken script, 10–15s
    segments: list[SegmentScript]           # ordered
    sign_off: str                           # spoken script, ~10s

class SegmentScript(TypedDict):
    agent: str                              # which agent's pitch this is
    pitch_title: str                        # from input, for traceability
    segue_in: str                           # transition from previous segment (empty for first)
    script: str                             # the spoken script for this segment
    estimated_length_sec: int               # LLM's estimate of spoken duration
```

**Producer does not emit `priority` or modify `Pitch` fields.** The `EpisodeScript` is a new object, not a mutation of the input pitches. Pitch objects remain immutable after agent emission.

### Producer memory (stub — deferred to learning-loop session)

Producer reads `producer_memory` at script-time. This memory tracks cross-episode ordering priors (e.g., "user tends to skip weather segments on weekends" → lower weather priority on Saturdays). The memory shape, write rules, and update signals are deferred to the learning-loop design session. For v0, `producer_memory` is an empty dict and the LLM operates without cross-episode priors.

**What Producer memory is NOT:** it is not per-agent memory. Producer never reads `AgentMemory` (per P9 — Producer is memory-blind to agent-level state). Producer's memory is about _episode-level_ patterns (ordering preferences, segment-skip patterns, time-of-day habits), not user taste.

### Rejected alternatives

| Alternative                                                                | Why rejected                                                                                                                                                                                                                      |
| -------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Fully LLM-driven segment selection (no deterministic prelude)              | Can't guarantee per-agent representation. The LLM might drop a low-priority agent entirely, contradicting the user's selection. The per-agent guarantee is a hard constraint, not a preference.                                   |
| Fully deterministic running-order (no LLM pass)                            | No segues, no today-threading, no narrative flow. Deterministic ordering produces a flat sequence with no connective tissue. The LLM's job is narration, not selection — selection is deterministic.                              |
| Producer rewrites agent hooks                                              | Covered in §3. Agent hooks are creative briefs, not scripts. Producer writes its own scripts _informed by_ hooks. Rewriting hooks blurs ownership and re-opens hallucination risk.                                                |
| Round-robin bonus slots (each agent gets a second before any gets a third) | Over-constrains selection. A user with rich youtube signal and thin weather signal should get more youtube content, not an artificial second weather pitch. Highest-remaining-priority across all agents is the right tiebreaker. |
| Agent hooks spoken verbatim in episode                                     | Agents don't know the show's voice, pacing, or today's context. Agent hooks are written for a different consumer (Producer) than the final listener. Producer translates taste-signal into radio.                                 |

---

## §5 Key decisions summary

| #   | Decision                     | Chosen                                                                                                                                                                                                                                                                                               | Rejected                                                                                            |
| --- | ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| 1   | Hook hallucination guardrail | Deterministic `claim_kind` computed pre-LLM, passed as constraint. 4 kinds: `durable`, `rising`, `discovery`, `neutral` with precondition floors on provenance counts.                                                                                                                               | Self-rubric LLM pass; pure prompt rules; post-hoc validator                                         |
| 2   | Asymmetric provenance shape  | `provenance_shape ∈ {balanced, sub_only, like_only}` computed at profile-build time. Single unified prompt template with per-shape directive block. Hook-fidelity rubric (5 axes × 0–2, ≥7/10 pass).                                                                                                 | Three branched templates; LLM-inferred shape; drop shape field                                      |
| 3   | Today's-radio currency       | `Brief.today_context` populated by weather + calendar agents in `fetch_context()`. Agents read it; Producer scripts with it. Agent hooks stay taste-only. Content discovery is Producer's job, scripted in segues/open/close. Two-phase fetch (weather+calendar first → Brief complete → all pitch). | Producer hook rewrite; `fresh_evidence` injection; agent-side search; Producer-side duplicate fetch |
| 4   | Producer running-order       | Two-step: deterministic prelude (1 guaranteed per agent + bonus by priority under time budget) → LLM writes full episode script (cold open, segments, segues, sign-off). Per-agent guarantee is hard. Producer memory is a read-only stub pending learning-loop session.                             | Fully LLM selection; fully deterministic order; round-robin bonus; verbatim agent hooks             |

## Dependencies on other components

| Component         | Contract from this doc                                                                                                            | Direction                                       |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| `agents/youtube`  | `claim_kind` + `provenance_shape` fields on `Pitch`; `compute_claim_kind()` + `compute_provenance_shape()` functions in algo step | extends youtube pitch()                         |
| `agents` (parent) | `Pitch` shape extended with `thin_signal`, `claim_kind`, `provenance_shape`                                                       | extends protocol                                |
| `Brief`           | `today_context: TodayContext` field added                                                                                         | extends Brief shape                             |
| `producer`        | `select_segments()` prelude + LLM script pass; `EpisodeScript` output schema; cannot drop/invent segments                         | new — this doc specs producer's prompting layer |
| `learning-loop`   | Producer memory shape — stub here, designed in learning-loop session                                                              | forward reference                               |
| `weather_agent`   | Returns `weather_summary` in `ScopeContext` from `fetch_context()`; orchestrator assembles into `Brief.today_context`             | new contract                                    |
| `calendar_agent`  | Returns `calendar_events` in `ScopeContext` from `fetch_context()`; orchestrator assembles into `Brief.today_context`             | new contract                                    |

## Test mandate (added 2026-04-16, eng review)

The deterministic functions in this doc are the guardrails that prevent hook hallucination. They must have unit tests before the LLM prompt step is built. Fixtures drawn from committed probe JSON at `tmp/ydata/probe_1776208130/`.

| Function | Test coverage required |
| --- | --- |
| `compute_claim_kind()` | All 4 claim kinds + evaluation order (first match wins) + the jazz-from-one-old-like scenario + `total_recent_weight` floor on `rising` |
| `compute_provenance_shape()` | All 3 shapes (balanced, sub_only, like_only) |
| `select_segments()` | Phase 1 guaranteed slots + Phase 2 bonus by priority + budget exhaustion + thin-signal pitch handling + `MAX_SEGMENT_SEC` clamping + cold-open-has-no-segue arithmetic + `DEFAULT_SEGMENT_SEC` applied when no overrides + `length_overrides` respected when provided |

## Open questions (parked)

- **Producer's "what's new" feed (v1+).** Producer currently scripts from `today_context` + pitch content only. v1 may add a trending/news feed so Producer can weave in "new Kamasi Washington album" around a jazz segment. Interface: Producer receives `fresh_content: list[ContentItem]` alongside pitches. Not built for v0.
- **Per-agent `suggested_length_sec` calibration.** v0 uses `DEFAULT_SEGMENT_SEC` (Producer-owned lookup). `select_segments()` accepts `length_overrides` so Producer memory can learn per-agent length biases and pass them in. Override mechanism is built; learning the biases is deferred to learning-loop.
- **Multi-user ordering priors.** v0 `producer_memory` is empty. When multi-user data exists, ordering priors (e.g., "morning users prefer weather first") could be seeded from aggregate patterns. Deferred to v1+.
- **Weather/calendar agent prompt design.** These agents are structurally simpler (structured input → pitch, no TF-IDF, no provenance compression) but their `pitch()` LLM calls still need prompt specs. Deferred to their respective design sessions. This doc specifies only their `today_context` population contract.

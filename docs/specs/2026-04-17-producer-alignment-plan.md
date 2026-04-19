# Producer Alignment Implementation Plan

> **Partially superseded (2026-04-18).** Code snippets in this plan include
> `rationale` on `Pitch` fixtures and in the Step-2 payload. Those were
> removed on 2026-04-18 as part of the agent-output conventions pass
> (rationale was write-only; priority / suggested_length_sec /
> provenance_shape / target_total_secs were dropped from the Step-2
> payload for the same reason). Unrelated `rationale` on `ExternalDecision`
> (see §Phase 2) is still live. Canonical current Pitch shape:
> `agents/protocol.py`. Canonical Step-2 payload: `producer/script.py`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `producer/` implementation up to the contracts documented in `producer/docs/DESIGN.md`, `agents/docs/prompt_design.md`, the master design (`wanlizhou-main-design-20260413-182237.md`), and `docs/specs/2026-04-17-producer-step2-prompt.md`. Eight discrepancies were locked in during the 2026-04-17 cross-check; this plan executes them in dependency order.

**Architecture:** Four phases, each independently shippable.

- **Phase 0 — Cleanup** (decisions 4a, 5a, 6.1a, 6.2b). Pure local edits in `producer/` + `agents/protocol.py`. New typed shapes, deterministic tie-break, DRY model/budget constants.
- **Phase 1 — Event bus** (decision 3d). New `producer/events.py` with in-process queue + JSONL stdout sink. Wire `producer.memory.applied`, `producer.selecting.{started,done}`, `producer.pick`. Sink is swappable; HTTP/SSE transport deferred to api-storage.
- **Phase 2 — External flow** (decision 1d). New `producer/external.py` with `decide_external_invocation`/`query_marketplace`/`select_external`. New `payment/stub.py` with mock `initiate_tx`. Orchestrator adds `AlicesAgent` and a second pitch round gated on the Producer's external decision. Emits `producer.external_decision.*`, `producer.marketplace.*`, `payment.*` events with the `phase: "internal"|"external"` field on `agent.pitching.*`.
- **Phase 3 — Per-segment streaming** (decision 2a). Refactor `producer/script.py` to `AsyncIterator[SegmentScript]`. Has a checkpoint task to decide audio integration shape (refactor `audio/orchestrator.py` or build a coordinator). Emits `script.segment.*` SSE events.

**Tech Stack:** Python 3.12+, `anthropic` SDK (Sonnet 4.6), `asyncio`, `pytest`, `TypedDict`, `concurrent.futures`. No new third-party deps.

**Memory references applied:**

- `feedback_producer_memory_deterministic.md` — ProducerMemory stays out of LLM prompts.
- `feedback_component_by_component_dev.md` — Phase 3's audio-boundary checkpoint asks the user before touching the audio component.
- `agentic_payment_pivot.md` — payment is mocked in Phase 2, real on-chain tx is its own follow-up.
- `feedback_read_docs_before_asking.md` — every task lists the spec section it implements.

---

## File structure

| Path                             | Phase   | Action     | Responsibility                                                                                           |
| -------------------------------- | ------- | ---------- | -------------------------------------------------------------------------------------------------------- | ------------------- |
| `agents/protocol.py`             | 0       | modify     | Add `RunningOrder`, `ExternalDecision`, `CreatorAgentListing` TypedDicts                                 |
| `producer/__init__.py`           | 0       | modify     | Export `DEFAULT_LLM_MODEL = "claude-sonnet-4-6"`                                                         |
| `producer/segments.py`           | 0       | modify     | Tie-break key; return `RunningOrder`; one-source `TARGET_EPISODE_SECS`                                   |
| `producer/bonus.py`              | 0       | modify     | Tie-break in fallback; import `DEFAULT_LLM_MODEL`; consume/return typed shape                            |
| `producer/script.py`             | 0/3     | modify     | Import `DEFAULT_LLM_MODEL` and `TARGET_EPISODE_SECS`; Phase 3 refactor to async iterator                 |
| `producer/events.py`             | 1       | **create** | In-process bus: `emit(name, payload)`, `subscribe(sink)`; default JSONL stdout sink                      |
| `producer/external.py`           | 2       | **create** | `decide_external_invocation`, `query_marketplace`, `select_external` (v0: always-invoke, hardcoded list) |
| `payment/__init__.py`            | 2       | **create** | Module marker                                                                                            |
| `payment/stub.py`                | 2       | **create** | `initiate_tx(from_wallet, to_wallet, amount_usdc) -> TxResult` (mock; honest about being mock)           |
| `agents/orchestrator.py`         | 0/1/2/3 | modify     | Wire `RunningOrder`, emit events, add second pitch round + payment, async streaming                      |
| `tests/test_protocol.py`         | 0       | **create** | Shape tests for new TypedDicts                                                                           |
| `tests/test_segments.py`         | 0       | modify     | Tie-break determinism; `RunningOrder` return                                                             |
| `tests/test_bonus_selection.py`  | 0       | modify     | Tie-break in fallback path                                                                               |
| `tests/test_producer_events.py`  | 1       | **create** | Bus + sink tests; emission ordering invariant                                                            |
| `tests/test_external.py`         | 2       | **create** | `decide`/`query`/`select_external` v0 behavior                                                           |
| `tests/test_payment_stub.py`     | 2       | **create** | Mock tx returns honest result                                                                            |
| `tests/test_script_streaming.py` | 3       | **create** | Async iterator emits segment 1 first; per-segment validation                                             |
| `producer/docs/DESIGN.md`        | all     | modify     | Update interface to reflect implemented shape                                                            |
| `agents/docs/DESIGN.md`          | 2       | modify     | Lock `phase: internal                                                                                    | external` SSE field |

---

## Phase 0 — Cleanup (decisions 4a, 5a, 6.1a, 6.2b)

Pre-condition: clean working tree (commit/stash anything in `agents/` and `producer/` first). Each task is one commit.

### Task 0.1: Define `RunningOrder`, `ExternalDecision`, `CreatorAgentListing` in `agents/protocol.py`

Implements decision 4a. Spec: `producer/docs/DESIGN.md:28-49`.

**Files:**

- Modify: `agents/protocol.py`
- Create: `tests/test_protocol.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_protocol.py`:

```python
"""Shape tests for new typed shapes added in 2026-04-17 producer alignment.

Spec: docs/specs/2026-04-17-producer-alignment-plan.md Task 0.1
      producer/docs/DESIGN.md §Interface contract
"""
from __future__ import annotations

from agents.protocol import (
    CreatorAgentListing,
    ExternalDecision,
    Pitch,
    RunningOrder,
)


def test_running_order_minimal_shape():
    pitch: Pitch = {
        "agent": "youtube", "title": "t", "hook": "h", "rationale": "r",
        "source_refs": [], "priority": 0.9, "thin_signal": False,
        "claim_kind": "neutral", "provenance_shape": "balanced",
        "suggested_length_sec": 90,
    }
    order: RunningOrder = {
        "segments": [pitch],
        "total_sec": 90,
        "guaranteed_count": 1,
        "bonus_count": 0,
    }
    assert order["segments"][0]["agent"] == "youtube"
    assert order["total_sec"] == 90


def test_external_decision_invoke_shape():
    decision: ExternalDecision = {"decision": "invoke", "rationale": "v0 always-invoke"}
    assert decision["decision"] == "invoke"


def test_external_decision_skip_shape():
    decision: ExternalDecision = {"decision": "skip", "rationale": "test"}
    assert decision["decision"] == "skip"


def test_creator_agent_listing_shape():
    listing: CreatorAgentListing = {
        "handle": "@GoddamnAxl",
        "display_name": "Alice's Lens",
        "scope": "tech / startup culture",
        "price_usdc": 0.10,
        "wallet_address": "0x8043AeeD92c681492B13f46e91EFb8B42D18E3b2",
    }
    assert listing["price_usdc"] == 0.10
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_protocol.py -v
```

Expected: `ImportError: cannot import name 'RunningOrder' from 'agents.protocol'`

- [ ] **Step 3: Add the shapes to `agents/protocol.py`**

Append after the existing `Pitch` definition:

```python
# ── RunningOrder: Producer's selected segments + episode-level metadata ──

class RunningOrder(TypedDict):
    """Producer's output of select_guaranteed_slots + select_bonus_segments_llm.

    Replaces the implicit `list[Pitch]` running-order shape used through 2026-04-17.
    The same Pitch objects appear under `segments`; the wrapper carries
    episode-level metadata that today's tuple returns smuggle separately.
    """
    segments: list[Pitch]              # ordered: guaranteed first, then bonus
    total_sec: int                     # sum of suggested_length_sec for all segments
    guaranteed_count: int              # how many of `segments` are guaranteed
    bonus_count: int                   # len(segments) - guaranteed_count


# ── ExternalDecision: Producer's call on whether to invoke an external agent ──

class ExternalDecision(TypedDict):
    """Result of producer.external.decide_external_invocation()."""
    decision: str                      # "invoke" | "skip"; v0 always "invoke"
    rationale: str                     # human-readable; surfaced in SSE event payload


# ── CreatorAgentListing: marketplace entry for an external agent ──

class CreatorAgentListing(TypedDict):
    """Result of producer.external.query_marketplace().

    v0 reads a hardcoded list. v1 queries a real marketplace.
    """
    handle: str                        # "@GoddamnAxl"
    display_name: str                  # "Alice's Lens"
    scope: str                         # human-readable scope description
    price_usdc: float                  # demo: 0.10
    wallet_address: str                # Base Sepolia address
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_protocol.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add agents/protocol.py tests/test_protocol.py
git commit -m "feat(protocol): add RunningOrder, ExternalDecision, CreatorAgentListing typed shapes

Locks in decision 4a from the 2026-04-17 producer alignment cross-check.
RunningOrder wraps the pre-existing implicit list[Pitch] return; the
external shapes pre-stage Phase 2 (external-flow wiring)."
```

---

### Task 0.2: Hoist `DEFAULT_LLM_MODEL` to `producer/__init__.py` and bump to `claude-sonnet-4-6`

Implements decision 6.1a. Latest Sonnet verified via web search 2026-04-17 = `claude-sonnet-4-6` (released 2026-02-17).

**Files:**

- Modify: `producer/__init__.py`
- Modify: `producer/bonus.py:56`
- Modify: `producer/script.py:39`

- [ ] **Step 1: Add the constant to `producer/__init__.py`**

Replace the current empty file content with:

```python
"""Producer component — running-order assembly + script generation.

Public constants are exported here so individual modules don't drift.
See producer/docs/DESIGN.md for component-level contract.
"""

DEFAULT_LLM_MODEL = "claude-sonnet-4-6"
"""Anthropic model used by Step 1.5 (bonus selection) and Step 2 (script generation).

Bumped from the pre-2026-04-17 default (claude-sonnet-4-20250514) per decision
6.1a in the producer alignment cross-check. Override via PRODUCER_LLM_MODEL env var.
"""
```

- [ ] **Step 2: Update `producer/bonus.py`**

Replace [bonus.py:56](producer/bonus.py#L56):

```python
# Before:
MODEL = os.environ.get("PRODUCER_LLM_MODEL", "claude-sonnet-4-20250514")

# After:
from producer import DEFAULT_LLM_MODEL
MODEL = os.environ.get("PRODUCER_LLM_MODEL", DEFAULT_LLM_MODEL)
```

(Move the `from producer import DEFAULT_LLM_MODEL` to the top-of-file imports group.)

- [ ] **Step 3: Update `producer/script.py`**

Same edit at [script.py:39](producer/script.py#L39):

```python
# Before:
MODEL = os.environ.get("PRODUCER_LLM_MODEL", "claude-sonnet-4-20250514")

# After:
from producer import DEFAULT_LLM_MODEL
MODEL = os.environ.get("PRODUCER_LLM_MODEL", DEFAULT_LLM_MODEL)
```

- [ ] **Step 4: Run the existing producer tests to confirm nothing broke**

```bash
pytest tests/test_segments.py tests/test_bonus_selection.py tests/test_producer_memory.py tests/test_script.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add producer/__init__.py producer/bonus.py producer/script.py
git commit -m "refactor(producer): hoist DEFAULT_LLM_MODEL, bump to claude-sonnet-4-6

Decision 6.1a from the 2026-04-17 producer alignment. The two MODEL
constants in bonus.py and script.py drifted independently; one source now."
```

---

### Task 0.3: One-source `TARGET_EPISODE_SECS`

Implements decision 6.2b.

**Files:**

- Modify: `producer/script.py:41`

- [ ] **Step 1: Replace the duplicate constant in `producer/script.py`**

```python
# Before (script.py line 41):
TARGET_EPISODE_SECS = 450

# After (replace with import; place in import group at top of file):
from producer.segments import TARGET_EPISODE_SECS
```

Drop the standalone declaration at line 41.

- [ ] **Step 2: Re-run script tests**

```bash
pytest tests/test_script.py -v
```

Expected: all green; `TARGET_EPISODE_SECS` resolves to 450 via import.

- [ ] **Step 3: Commit**

```bash
git add producer/script.py
git commit -m "refactor(producer): import TARGET_EPISODE_SECS from segments.py

Decision 6.2b. The constant was declared in two places; segments.py is
the canonical home (it owns all budget arithmetic)."
```

---

### Task 0.4: Deterministic tie-breaking in `select_guaranteed_slots`

Implements decision 5a. Spec: `producer/docs/DESIGN.md:310`.

**Files:**

- Modify: `producer/segments.py:70`
- Modify: `tests/test_segments.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_segments.py`:

```python
class TestTieBreakDeterminism:
    """Decision 5a: ties resolve by (-priority, agent, title) — reproducible."""

    def test_same_priority_within_agent_resolves_by_title_asc(self):
        # Two pitches at identical priority within one agent.
        # DESIGN: deterministic by title ASC as final tiebreaker.
        pitches = {
            "youtube": [
                _pitch("youtube", "zebra", 0.9),
                _pitch("youtube", "alpha", 0.9),  # same priority, alpha < zebra
            ],
        }
        guaranteed, _, _ = select_guaranteed_slots(pitches)
        assert guaranteed[0]["title"] == "alpha"

    def test_same_priority_across_agents_resolves_by_agent_asc(self):
        # Two agents with their top pitch tied — agent name ASC wins.
        pitches = {
            "youtube": [_pitch("youtube", "y", 0.7)],
            "calendar": [_pitch("calendar", "c", 0.7)],
        }
        guaranteed, _, _ = select_guaranteed_slots(pitches)
        # Both win their guaranteed slots (one per agent), but iteration
        # order in `guaranteed` should be deterministic by agent ASC.
        agents = [p["agent"] for p in guaranteed]
        assert agents == sorted(agents)  # ["calendar", "youtube"]
```

(Reuse the `_pitch` helper from existing tests — copy from `tests/test_producer_memory.py:59-71` if not already present.)

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_segments.py::TestTieBreakDeterminism -v
```

Expected: at least one fails (depends on dict insertion order).

- [ ] **Step 3: Update `producer/segments.py`**

Change [segments.py:70](producer/segments.py#L70) inside `select_guaranteed_slots`:

```python
# Before:
for agent, pitches in pitches_by_agent.items():
    best = max(pitches, key=lambda p: p["priority"])

# After:
# Sort agents deterministically so iteration order doesn't depend on dict insertion.
for agent in sorted(pitches_by_agent):
    pitches = pitches_by_agent[agent]
    # Deterministic max: highest priority, then title ASC as final tiebreaker.
    best = min(pitches, key=lambda p: (-p["priority"], p["title"]))
```

(Using `min` with negated priority + title gives the same "highest priority, ties by title ASC" semantics deterministically.)

- [ ] **Step 4: Run tests; confirm both new and existing pass**

```bash
pytest tests/test_segments.py tests/test_producer_memory.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add producer/segments.py tests/test_segments.py
git commit -m "fix(producer): deterministic tie-breaking in select_guaranteed_slots

Decision 5a from 2026-04-17 alignment. Iteration order was dict-insertion
dependent; now sorted by agent ASC, with title ASC as within-agent tiebreaker."
```

---

### Task 0.5: Deterministic tie-breaking in `_fallback_bonus_selection`

Same fix at the second sort site.

**Files:**

- Modify: `producer/bonus.py:204`
- Modify: `tests/test_bonus_selection.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_bonus_selection.py`:

```python
class TestFallbackTieBreakDeterminism:
    """Decision 5a: fallback path picks ties by (-priority, agent, title)."""

    def test_fallback_resolves_cross_agent_ties_by_agent_asc(self, monkeypatch):
        # Force fallback path.
        monkeypatch.setenv("DISABLE_LLM", "1")
        guaranteed = []
        # Two pitches, identical priority, different agents.
        # Budget allows ONE bonus pick.
        remaining = [
            _pitch("youtube", "y1", 0.5, seg_len=40),
            _pitch("calendar", "c1", 0.5, seg_len=40),
        ]
        from producer.bonus import select_bonus_segments_llm
        bonus, _ = select_bonus_segments_llm(
            guaranteed_slots=guaranteed,
            remaining_pitches=remaining,
            budget_remaining_sec=50,  # exactly one (40 + 10 segue)
            today_context={
                "date": "2026-04-17", "day_of_week": "Thursday",
                "time_of_day": "morning", "weather_summary": None,
                "calendar_events": None,
            },
        )
        assert len(bonus) == 1
        # calendar < youtube alphabetically, so calendar wins.
        assert bonus[0]["agent"] == "calendar"
```

(`_pitch` helper as in Task 0.4.)

- [ ] **Step 2: Run to verify it fails (or passes only by accident)**

```bash
pytest tests/test_bonus_selection.py::TestFallbackTieBreakDeterminism -v
```

Expected: depends on insertion order; even if it passes today, the protection is what we want.

- [ ] **Step 3: Update `producer/bonus.py:204`**

```python
# Before:
for pitch in sorted(remaining_pitches, key=lambda p: p["priority"], reverse=True):

# After:
# Decision 5a: deterministic across-agent tiebreaking.
for pitch in sorted(
    remaining_pitches,
    key=lambda p: (-p["priority"], p["agent"], p["title"]),
):
```

- [ ] **Step 4: Run all producer tests**

```bash
pytest tests/test_segments.py tests/test_bonus_selection.py tests/test_producer_memory.py tests/test_script.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add producer/bonus.py tests/test_bonus_selection.py
git commit -m "fix(producer): deterministic tie-break in bonus fallback

Decision 5a applied to the second sort site. Now across-agent ties resolve
by agent ASC, title ASC — same rule as select_guaranteed_slots."
```

---

### Task 0.6: Refactor `select_guaranteed_slots` to return a `RunningOrder`

Implements the 4a refactor. Wraps existing tuple return in the typed shape; bonus.py + orchestrator + tests follow.

**Files:**

- Modify: `producer/segments.py`
- Modify: `producer/bonus.py`
- Modify: `agents/orchestrator.py`
- Modify: `producer/tmp/test_integration.py`
- Modify: `tests/test_segments.py`
- Modify: `tests/test_producer_memory.py` (the `TestPipelineBonusSlotFlip._run_pipeline` helper)

This is a refactor with broad reach. We do it in one task because the type change cascades; splitting would leave the codebase in an uncompilable state between commits.

- [ ] **Step 1: Update `select_guaranteed_slots` signature and return**

In `producer/segments.py`, change the function:

```python
from agents.protocol import Pitch, RunningOrder


def select_guaranteed_slots(
    pitches_by_agent: dict[str, list[Pitch]],
    length_overrides: dict[str, int] | None = None,
) -> tuple[RunningOrder, list[Pitch], int]:
    """Phase 1 (deterministic): one guaranteed slot per agent.

    Returns:
        order: RunningOrder with `segments` = guaranteed slots only,
            `bonus_count = 0`. Step 1.5 will return an updated RunningOrder
            with bonus segments appended.
        remaining: unselected pitches with suggested_length_sec set.
        budget_remaining_sec: seconds available for Step 1.5.

    See decision 4a in docs/specs/2026-04-17-producer-alignment-plan.md.
    """
    guaranteed: list[Pitch] = []
    remaining: list[Pitch] = []

    for agent in sorted(pitches_by_agent):
        pitches = pitches_by_agent[agent]
        best = min(pitches, key=lambda p: (-p["priority"], p["title"]))
        guaranteed.append(
            {**best, "suggested_length_sec": _segment_length(best, length_overrides)}
        )
        for p in pitches:
            if p is best:
                continue
            remaining.append(
                {**p, "suggested_length_sec": _segment_length(p, length_overrides)}
            )

    budget = TARGET_EPISODE_SECS - OPEN_CLOSE_SECS
    budget -= sum(p["suggested_length_sec"] for p in guaranteed)
    budget -= SEGUE_OVERHEAD_SECS * max(0, len(guaranteed) - 1)

    order: RunningOrder = {
        "segments": guaranteed,
        "total_sec": sum(p["suggested_length_sec"] for p in guaranteed),
        "guaranteed_count": len(guaranteed),
        "bonus_count": 0,
    }
    return order, remaining, budget
```

- [ ] **Step 2: Add a helper for adding bonus segments to a RunningOrder**

Append to `producer/segments.py`:

```python
def append_bonus(order: RunningOrder, bonus: list[Pitch]) -> RunningOrder:
    """Pure: returns a new RunningOrder with `bonus` appended to segments."""
    new_segments = order["segments"] + bonus
    return {
        "segments": new_segments,
        "total_sec": sum(p["suggested_length_sec"] for p in new_segments),
        "guaranteed_count": order["guaranteed_count"],
        "bonus_count": len(bonus),
    }
```

- [ ] **Step 3: Update `select_bonus_segments_llm` callers to use `RunningOrder`**

In `producer/bonus.py`, the function signature stays the same (it takes guaranteed*slots: list[Pitch] and returns bonus_pitches + reasons). The change is the \_callers* now pass `order["segments"]` and assemble the final RunningOrder via `append_bonus`. Add this docstring note:

```python
def select_bonus_segments_llm(
    guaranteed_slots: list[Pitch],
    ...
) -> tuple[list[Pitch], list[PickReason]]:
    """...
    Note: returns plain list[Pitch] for bonus segments. Callers wrap into
    a RunningOrder via producer.segments.append_bonus().
    """
```

(No code change to `bonus.py` beyond the docstring — the typed shape lives outside the LLM-facing API.)

- [ ] **Step 4: Update `agents/orchestrator.py` to consume the new return**

In `agents/orchestrator.py:178-200` (the CLI section), replace:

```python
guaranteed, remaining, bonus_budget = select_guaranteed_slots(pitches_by_agent)
print(f"── Guaranteed slots ({len(guaranteed)}; {bonus_budget}s bonus budget) ──")
for p in guaranteed:
    print(f"  [{p['agent']}] {p['title']} ({p['suggested_length_sec']}s)")
print()

from producer.bonus import select_bonus_segments_llm
bonus, guaranteed_reasons = select_bonus_segments_llm(
    guaranteed_slots=guaranteed, ...
)
selected = guaranteed + bonus
```

with:

```python
order, remaining, bonus_budget = select_guaranteed_slots(pitches_by_agent)
print(f"── Guaranteed slots ({order['guaranteed_count']}; {bonus_budget}s bonus budget) ──")
for p in order["segments"]:
    print(f"  [{p['agent']}] {p['title']} ({p['suggested_length_sec']}s)")
print()

from producer.bonus import select_bonus_segments_llm
from producer.segments import append_bonus
bonus, guaranteed_reasons = select_bonus_segments_llm(
    guaranteed_slots=order["segments"],
    remaining_pitches=remaining,
    budget_remaining_sec=bonus_budget,
    today_context=brief["today_context"],
)
order = append_bonus(order, bonus)
selected = order["segments"]
```

- [ ] **Step 5: Update `producer/tmp/test_integration.py` (lines 119, 162) the same way**

```python
# Before line 119:
guaranteed, remaining, bonus_budget = select_guaranteed_slots(pitches_by_agent)
# After:
order, remaining, bonus_budget = select_guaranteed_slots(pitches_by_agent)
guaranteed = order["segments"]  # back-compat for the print loop below
# (no other line-by-line changes; the existing `selected = guaranteed + bonus` at line 162 still works)
```

- [ ] **Step 6: Update `tests/test_producer_memory.py` `_run_pipeline` helper (line 244-258)**

```python
# Before:
guaranteed, remaining, _ = select_guaranteed_slots(adjusted, length_overrides=length_overrides)

# After:
order, remaining, _ = select_guaranteed_slots(adjusted, length_overrides=length_overrides)
guaranteed = order["segments"]
```

- [ ] **Step 7: Update `tests/test_segments.py` to assert on `RunningOrder` shape**

Add at least one assertion:

```python
def test_select_guaranteed_returns_running_order_shape(self):
    pitches = {"youtube": [_pitch("youtube", "a", 0.9, seg_len=90)]}
    order, _, _ = select_guaranteed_slots(pitches)
    assert order["guaranteed_count"] == 1
    assert order["bonus_count"] == 0
    assert order["total_sec"] == 90
    assert order["segments"][0]["title"] == "a"
```

- [ ] **Step 8: Full producer test pass**

```bash
pytest tests/test_protocol.py tests/test_segments.py tests/test_bonus_selection.py tests/test_producer_memory.py tests/test_script.py -v
```

Expected: all green.

- [ ] **Step 9: Smoke-test integration**

```bash
DISABLE_LLM=1 python -m agents.orchestrator --no-llm
```

Expected: prints valid running-order JSON without errors.

- [ ] **Step 10: Commit**

```bash
git add agents/protocol.py agents/orchestrator.py producer/segments.py producer/bonus.py producer/tmp/test_integration.py tests/test_segments.py tests/test_producer_memory.py
git commit -m "refactor(producer): assemble typed RunningOrder, end of Phase 0

Decision 4a from the 2026-04-17 alignment. select_guaranteed_slots now
returns a typed RunningOrder; orchestrator + integration test updated.
append_bonus() is the pure helper for Step 1.5 results."
```

---

## Phase 1 — Event bus (decision 3d)

In-process event bus + JSONL stdout sink. Wire the four producer events the DESIGN spec requires. SSE/HTTP transport stays deferred to api-storage.

### Task 1.1: Build `producer/events.py`

**Files:**

- Create: `producer/events.py`
- Create: `tests/test_producer_events.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_producer_events.py`:

```python
"""Tests for producer/events.py — in-process bus + sinks.

Spec: docs/specs/2026-04-17-producer-alignment-plan.md Phase 1
      producer/docs/DESIGN.md §SSE
"""
from __future__ import annotations

import io
import json

import pytest

from producer.events import (
    EventBus,
    JsonlSink,
    emit,
    set_default_bus,
    subscribe,
)


def test_emit_with_no_subscribers_is_silent():
    bus = EventBus()
    bus.emit("producer.test", {"k": 1})  # no exception


def test_subscribe_receives_emissions():
    bus = EventBus()
    received = []
    bus.subscribe(lambda name, payload: received.append((name, payload)))
    bus.emit("producer.test", {"k": 1})
    assert received == [("producer.test", {"k": 1})]


def test_emission_order_preserved_across_multiple_emits():
    bus = EventBus()
    received = []
    bus.subscribe(lambda name, payload: received.append(name))
    bus.emit("producer.memory.applied", {})
    bus.emit("producer.selecting.started", {})
    bus.emit("producer.pick", {})
    bus.emit("producer.selecting.done", {})
    assert received == [
        "producer.memory.applied",
        "producer.selecting.started",
        "producer.pick",
        "producer.selecting.done",
    ]


def test_jsonl_sink_writes_one_line_per_event():
    buf = io.StringIO()
    sink = JsonlSink(buf)
    sink("producer.test", {"k": 1})
    sink("producer.test", {"k": 2})
    lines = buf.getvalue().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"event": "producer.test", "payload": {"k": 1}}
    assert json.loads(lines[1]) == {"event": "producer.test", "payload": {"k": 2}}


def test_module_level_emit_routes_to_default_bus():
    bus = EventBus()
    received = []
    bus.subscribe(lambda name, payload: received.append((name, payload)))
    set_default_bus(bus)
    emit("producer.test", {"k": 1})
    assert received == [("producer.test", {"k": 1})]


def test_subscribe_module_level_adds_to_default_bus():
    bus = EventBus()
    received = []
    set_default_bus(bus)
    subscribe(lambda n, p: received.append(n))
    emit("producer.test", {})
    assert received == ["producer.test"]
```

- [ ] **Step 2: Run test to verify import error**

```bash
pytest tests/test_producer_events.py -v
```

Expected: `ModuleNotFoundError: No module named 'producer.events'`.

- [ ] **Step 3: Create `producer/events.py`**

```python
"""In-process event bus for Producer SSE-bound events.

Sinks consume `(event_name, payload)` tuples. Default sink is JSONL to stdout
during dev/CLI runs; the api-storage component can replace the sink with one
that ships events over HTTP/SSE.

The bus is intentionally thread-safe-by-stupid (uses a list, not a lock):
producer events are emitted from the main thread between phases, never from
inside the parallel pitch round. If that changes, wrap subscribers in a lock.

Spec: producer/docs/DESIGN.md §SSE
      docs/specs/2026-04-17-producer-alignment-plan.md Phase 1 (decision 3d)
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import IO, TextIO

EventSink = Callable[[str, dict], None]


class EventBus:
    """Tiny pub/sub. Subscribers are functions of (event_name, payload)."""

    def __init__(self) -> None:
        self._subscribers: list[EventSink] = []

    def subscribe(self, sink: EventSink) -> None:
        self._subscribers.append(sink)

    def emit(self, name: str, payload: dict) -> None:
        for sink in self._subscribers:
            sink(name, payload)


class JsonlSink:
    """Writes one JSON line per event to a file-like (default: stdout).

    Wire format: {"event": "<name>", "payload": <payload>}
    """

    def __init__(self, stream: IO[str] | TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def __call__(self, name: str, payload: dict) -> None:
        self._stream.write(json.dumps({"event": name, "payload": payload}) + "\n")
        self._stream.flush()


# ── Module-level convenience ──────────────────────────────────────────

_default_bus = EventBus()


def set_default_bus(bus: EventBus) -> None:
    """Replace the module-level default bus (test seam)."""
    global _default_bus
    _default_bus = bus


def emit(name: str, payload: dict) -> None:
    """Emit on the module-level default bus."""
    _default_bus.emit(name, payload)


def subscribe(sink: EventSink) -> None:
    """Subscribe a sink to the module-level default bus."""
    _default_bus.subscribe(sink)
```

- [ ] **Step 4: Run tests, confirm green**

```bash
pytest tests/test_producer_events.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add producer/events.py tests/test_producer_events.py
git commit -m "feat(producer): in-process event bus with JSONL stdout sink

Decision 3d from 2026-04-17 alignment. Producer can now emit() events
through a swappable sink. Default JSONL/stdout means CLI runs immediately
show the structured event trace; api-storage can replace the sink without
touching call sites."
```

---

### Task 1.2: Wire `producer.memory.applied` from `producer/memory.py`

**Files:**

- Modify: `producer/memory.py`
- Modify: `agents/orchestrator.py`
- Modify: `tests/test_producer_memory.py`

The builder already exists ([memory.py:175-206](producer/memory.py#L175-L206)). We add a thin `emit_memory_applied(...)` that calls the builder and emits if non-None.

- [ ] **Step 1: Add the emitter**

Append to `producer/memory.py`:

```python
from producer.events import emit


def emit_memory_applied(
    memory: ProducerMemory,
    raw_pitches_by_agent: dict[str, list[Pitch]],
    adjusted_pitches_by_agent: dict[str, list[Pitch]],
) -> None:
    """Build and emit `producer.memory.applied` if memory is non-empty.

    Per producer/docs/DESIGN.md §SSE: silent (no event) for bootstrap-fresh
    users — the identity transform produces no event on the trace.
    """
    payload = build_memory_applied_event(
        memory, raw_pitches_by_agent, adjusted_pitches_by_agent
    )
    if payload is not None:
        emit("producer.memory.applied", payload)
```

- [ ] **Step 2: Update orchestrator to call the emitter**

In `agents/orchestrator.py:155-173` (CLI section), replace the `build_memory_applied_event` + `print` block with:

```python
from producer.memory import (
    apply_producer_memory,
    emit_memory_applied,
    load_producer_memory,
)

producer_memory = load_producer_memory(args.user_id)
raw_pitches_by_agent = pitches_by_agent
pitches_by_agent = apply_producer_memory(pitches_by_agent, producer_memory)
emit_memory_applied(producer_memory, raw_pitches_by_agent, pitches_by_agent)
```

The default JSONL/stdout sink will write the event automatically — no `print()` call needed.

- [ ] **Step 3: Add a test for the emitter**

Append to `tests/test_producer_memory.py` (in `TestBuildMemoryAppliedEvent`):

```python
def test_emit_memory_applied_silent_when_weights_empty(self):
    """Bootstrap users emit nothing — silent identity transform."""
    from producer.events import EventBus, set_default_bus
    bus = EventBus()
    captured = []
    bus.subscribe(lambda n, p: captured.append((n, p)))
    set_default_bus(bus)
    raw = {"youtube": [_pitch("youtube", "a", 0.9)]}
    adj = raw
    from producer.memory import emit_memory_applied
    emit_memory_applied(bootstrap_producer_memory(), raw, adj)
    assert captured == []

def test_emit_memory_applied_fires_when_weights_present(self):
    from producer.events import EventBus, set_default_bus
    bus = EventBus()
    captured = []
    bus.subscribe(lambda n, p: captured.append((n, p)))
    set_default_bus(bus)
    raw = {"youtube": [_pitch("youtube", "a", 0.9)]}
    adj = apply_producer_memory(raw, _mem({"youtube": 1.5}))
    from producer.memory import emit_memory_applied
    emit_memory_applied(_mem({"youtube": 1.5}), raw, adj)
    assert len(captured) == 1
    assert captured[0][0] == "producer.memory.applied"
    assert captured[0][1]["agent_weights"] == {"youtube": 1.5}
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_producer_memory.py tests/test_producer_events.py -v
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add producer/memory.py agents/orchestrator.py tests/test_producer_memory.py
git commit -m "feat(producer): emit producer.memory.applied via event bus

Decision 3d. Replaces the orchestrator's print(json.dumps(...)) with
emit() through producer/events.py — JSONL-to-stdout sink is the default."
```

---

### Task 1.3: Wire `producer.selecting.{started,done}` and `producer.pick`

**Files:**

- Modify: `producer/bonus.py`
- Modify: `agents/orchestrator.py`
- Modify: `tests/test_bonus_selection.py`

The pattern: `select_bonus_segments_llm` itself stays a pure function. The orchestrator wraps the call, emitting events around it. This keeps `bonus.py` test-isolatable.

- [ ] **Step 1: Add a thin event-emitting wrapper in `producer/bonus.py`**

Append to `producer/bonus.py`:

```python
from producer.events import emit


def select_bonus_with_events(
    guaranteed_slots: list[Pitch],
    remaining_pitches: list[Pitch],
    budget_remaining_sec: int,
    today_context: TodayContext,
    segue_overhead_sec: int = SEGUE_OVERHEAD_SECS,
    length_overrides: dict[str, int] | None = None,
) -> tuple[list[Pitch], list[PickReason]]:
    """Same as select_bonus_segments_llm but emits Step 1.5 SSE events.

    Spec: agents/docs/prompt_design.md §4 Step 1.5 SSE integration
          producer/docs/DESIGN.md §SSE
    """
    bonus, guaranteed_reasons = select_bonus_segments_llm(
        guaranteed_slots=guaranteed_slots,
        remaining_pitches=remaining_pitches,
        budget_remaining_sec=budget_remaining_sec,
        today_context=today_context,
        segue_overhead_sec=segue_overhead_sec,
        length_overrides=length_overrides,
    )

    overall = (
        guaranteed_reasons[0]["reasoning_summary"][:80]
        if guaranteed_reasons else "selecting segments by priority within time budget"
    )
    emit("producer.selecting.started", {"reasoning_summary": overall})

    for slot, reason in zip(guaranteed_slots, guaranteed_reasons):
        emit("producer.pick", {
            "agent": slot["agent"],
            "pitch_title": slot["title"],
            "allocated_sec": slot["suggested_length_sec"],
            "reasoning_summary": reason["reasoning_summary"],
            "kind": "guaranteed",
        })
    for b in bonus:
        emit("producer.pick", {
            "agent": b["agent"],
            "pitch_title": b["title"],
            "allocated_sec": b["suggested_length_sec"],
            "reasoning_summary": b.get("reasoning_summary", ""),
            "kind": "bonus",
        })

    total_sec = sum(p["suggested_length_sec"] for p in guaranteed_slots + bonus)
    emit("producer.selecting.done", {
        "running_order_titles": [p["title"] for p in guaranteed_slots + bonus],
        "reasoning_summary": f"{len(guaranteed_slots) + len(bonus)} segments, {total_sec}s allocated",
    })

    return bonus, guaranteed_reasons
```

- [ ] **Step 2: Update orchestrator to call the wrapper**

In `agents/orchestrator.py`, change the import and call site:

```python
# Before:
from producer.bonus import select_bonus_segments_llm
bonus, guaranteed_reasons = select_bonus_segments_llm(...)

# After:
from producer.bonus import select_bonus_with_events
bonus, guaranteed_reasons = select_bonus_with_events(...)
```

- [ ] **Step 3: Add a test asserting the four events fire in order**

Append to `tests/test_bonus_selection.py`:

```python
class TestStepOnePointFiveSSE:
    """Decision 3d: producer.selecting.{started,done} + producer.pick events."""

    def test_emits_started_then_picks_then_done(self, monkeypatch):
        monkeypatch.setenv("DISABLE_LLM", "1")
        from producer.events import EventBus, set_default_bus
        from producer.bonus import select_bonus_with_events
        bus = EventBus()
        captured = []
        bus.subscribe(lambda n, p: captured.append((n, p)))
        set_default_bus(bus)
        guaranteed = [_pitch("youtube", "yt-1", 0.9, seg_len=90)]
        remaining = [_pitch("youtube", "yt-2", 0.7, seg_len=40)]
        select_bonus_with_events(
            guaranteed_slots=guaranteed,
            remaining_pitches=remaining,
            budget_remaining_sec=50,
            today_context={
                "date": "2026-04-17", "day_of_week": "Thursday",
                "time_of_day": "morning", "weather_summary": None,
                "calendar_events": None,
            },
        )
        names = [n for n, _ in captured]
        assert names[0] == "producer.selecting.started"
        assert names[-1] == "producer.selecting.done"
        assert all(n == "producer.pick" for n in names[1:-1])
        assert len(names) == 1 + 1 + 1 + 1  # started + 1 guaranteed + 1 bonus + done
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_bonus_selection.py tests/test_producer_events.py -v
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add producer/bonus.py agents/orchestrator.py tests/test_bonus_selection.py
git commit -m "feat(producer): emit Step 1.5 SSE events via select_bonus_with_events

Decision 3d, second wire site. select_bonus_segments_llm stays a pure
function; the wrapper emits producer.selecting.{started,done} + per-pick
events around it. Orchestrator switches to the wrapper."
```

---

### Task 1.4: Update `producer/docs/DESIGN.md` SSE section

**Files:**

- Modify: `producer/docs/DESIGN.md`

- [ ] **Step 1: Mark the SSE section as implemented (no code change)**

In `producer/docs/DESIGN.md` near line 288 (`#### SSE: producer.memory.applied`), add:

```markdown
> **Implementation status (2026-04-17):** Implemented via `producer/events.py`
> bus + JSONL/stdout sink. `producer.memory.applied`, `producer.selecting.started`,
> `producer.pick`, `producer.selecting.done` are all live. HTTP/SSE transport
> deferred to api-storage; sink swap is the only change required when api-storage lands.
```

- [ ] **Step 2: Commit**

```bash
git add producer/docs/DESIGN.md
git commit -m "docs(producer): mark Phase 1 SSE wiring complete in DESIGN.md"
```

---

## Phase 2 — External flow (decision 1d)

`AlicesAgent` already exists and is used by `producer/tmp/test_integration.py`. This phase wires Producer's external decision + marketplace + payment around it, then plumbs `AlicesAgent` into the production orchestrator's second pitch round.

### Task 2.1: Build `payment/stub.py`

**Files:**

- Create: `payment/__init__.py` (empty marker)
- Create: `payment/stub.py`
- Create: `tests/test_payment_stub.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_payment_stub.py`:

```python
"""Tests for payment/stub.py — mock agentic payment.

Spec: docs/specs/2026-04-17-producer-alignment-plan.md Phase 2 (decision 1d)
      Master design wanlizhou-main-design-20260413-182237.md §Agentic payment
"""
from __future__ import annotations

from payment.stub import TxResult, initiate_tx


def test_initiate_tx_returns_mock_result():
    result = initiate_tx(
        from_wallet="0xPRODUCER",
        to_wallet="0x8043AeeD92c681492B13f46e91EFb8B42D18E3b2",
        amount_usdc=0.10,
    )
    assert isinstance(result, dict)
    assert result["mode"] == "MOCK"
    assert result["amount_usdc"] == 0.10
    assert result["to_wallet"] == "0x8043AeeD92c681492B13f46e91EFb8B42D18E3b2"
    assert result["tx_hash"].startswith("0xMOCK")
    assert result["basescan_url"] == ""


def test_mock_tx_hash_is_deterministic_per_arguments():
    a = initiate_tx("0xA", "0xB", 0.10)
    b = initiate_tx("0xA", "0xB", 0.10)
    assert a["tx_hash"] == b["tx_hash"]
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_payment_stub.py -v
```

Expected: `ModuleNotFoundError: payment.stub`.

- [ ] **Step 3: Create `payment/__init__.py`**

```python
"""Payment component — stub today, real on-chain tx is its own follow-up.

See docs/specs/2026-04-17-producer-alignment-plan.md Phase 2 for context.
"""
```

- [ ] **Step 4: Create `payment/stub.py`**

```python
"""Mock payment.initiate_tx for v0 demo.

Returns deterministic mock TxResult dicts. Honest about being a mock —
`mode = "MOCK"`, tx_hash is `0xMOCK<hash>`, basescan_url is empty.

Real on-chain tx via viem on Base Sepolia is its own follow-up; this
unblocks the Producer external-flow plumbing in Phase 2 of the
2026-04-17 alignment plan (decision 1d).
"""
from __future__ import annotations

import hashlib
from typing import TypedDict


class TxResult(TypedDict):
    mode: str              # "MOCK" | "LIVE" — wire format spec'd by master design
    tx_hash: str           # 0x-prefixed; "0xMOCK" prefix when mode == "MOCK"
    basescan_url: str      # "" for mock; populated when real
    amount_usdc: float
    from_wallet: str
    to_wallet: str


def initiate_tx(from_wallet: str, to_wallet: str, amount_usdc: float) -> TxResult:
    """Stub: returns a deterministic mock TxResult.

    Hash is sha256(from|to|amount) so the same inputs produce the same hash —
    keeps test fixtures stable. NOT cryptographically meaningful.
    """
    raw = f"{from_wallet}|{to_wallet}|{amount_usdc}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:32]
    return TxResult(
        mode="MOCK",
        tx_hash=f"0xMOCK{digest}",
        basescan_url="",
        amount_usdc=amount_usdc,
        from_wallet=from_wallet,
        to_wallet=to_wallet,
    )
```

- [ ] **Step 5: Run test, confirm green**

```bash
pytest tests/test_payment_stub.py -v
```

- [ ] **Step 6: Commit**

```bash
git add payment/__init__.py payment/stub.py tests/test_payment_stub.py
git commit -m "feat(payment): mock initiate_tx stub for Phase 2 external flow

Decision 1d. Honest mock — TxResult.mode='MOCK', tx_hash='0xMOCK<sha>'.
Real on-chain tx via viem is its own follow-up."
```

---

### Task 2.2: Build `producer/external.py`

**Files:**

- Create: `producer/external.py`
- Create: `tests/test_external.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_external.py`:

```python
"""Tests for producer/external.py — external-agent invocation flow.

Spec: producer/docs/DESIGN.md §Interface contract
      docs/specs/2026-04-17-producer-alignment-plan.md Phase 2
"""
from __future__ import annotations

from producer.external import (
    decide_external_invocation,
    query_marketplace,
    select_external,
)


def test_decide_external_invocation_v0_always_invokes():
    pitches = {"youtube": [{"agent": "youtube", "title": "t", "priority": 0.9}]}
    decision = decide_external_invocation(pitches)  # type: ignore[arg-type]
    assert decision["decision"] == "invoke"
    assert decision["rationale"]


def test_query_marketplace_returns_hardcoded_listings():
    listings = query_marketplace()
    assert len(listings) >= 1
    handles = {l["handle"] for l in listings}
    assert "@GoddamnAxl" in handles


def test_select_external_returns_alices_in_v0():
    listings = query_marketplace()
    chosen = select_external(listings, brief={
        "today_context": {
            "date": "2026-04-17", "day_of_week": "Thursday",
            "time_of_day": "morning", "weather_summary": None,
            "calendar_events": None,
        }
    })
    assert chosen["handle"] == "@GoddamnAxl"
    assert chosen["price_usdc"] == 0.10
```

- [ ] **Step 2: Run, confirm import error**

```bash
pytest tests/test_external.py -v
```

- [ ] **Step 3: Create `producer/external.py`**

```python
"""Producer external-agent invocation flow (v0: always-invoke + hardcoded list).

Three pure functions:
  decide_external_invocation(pitches) -> ExternalDecision
  query_marketplace()                 -> list[CreatorAgentListing]
  select_external(candidates, brief)  -> CreatorAgentListing

Spec: producer/docs/DESIGN.md §Interface contract (lines 28-49)
      Master design §10 (Alice's agent invoked by Producer)
      docs/specs/2026-04-17-producer-alignment-plan.md Phase 2 (decision 1d)
"""
from __future__ import annotations

from agents.protocol import (
    Brief,
    CreatorAgentListing,
    ExternalDecision,
    Pitch,
)


# v0: hardcoded list. v1: real marketplace query.
_MARKETPLACE_V0: list[CreatorAgentListing] = [
    {
        "handle": "@GoddamnAxl",
        "display_name": "Alice's Lens",
        "scope": "tech / startup culture",
        "price_usdc": 0.10,
        "wallet_address": "0x8043AeeD92c681492B13f46e91EFb8B42D18E3b2",
    },
]


def decide_external_invocation(
    pitches_by_agent: dict[str, list[Pitch]],
) -> ExternalDecision:
    """v0: always invokes. Returns {decision: 'invoke', rationale: ...}.

    v1 may condition on topic-cluster entropy / cocoon detection per
    producer/docs/DESIGN.md interface contract.
    """
    return ExternalDecision(
        decision="invoke",
        rationale="v0 anti-cocoon policy: always bring an outside voice",
    )


def query_marketplace() -> list[CreatorAgentListing]:
    """v0: returns the hardcoded marketplace list."""
    return list(_MARKETPLACE_V0)


def select_external(
    candidates: list[CreatorAgentListing],
    brief: Brief,
) -> CreatorAgentListing:
    """v0: returns @GoddamnAxl (the only listing that matches seed topics).

    Brief is accepted for v1 forward-compat; v0 ignores it.
    """
    _ = brief
    if not candidates:
        raise ValueError("query_marketplace returned no candidates")
    for c in candidates:
        if c["handle"] == "@GoddamnAxl":
            return c
    return candidates[0]
```

- [ ] **Step 4: Tests green**

```bash
pytest tests/test_external.py -v
```

- [ ] **Step 5: Commit**

```bash
git add producer/external.py tests/test_external.py
git commit -m "feat(producer): external invocation + marketplace + select_external

Decision 1d, Phase 2 core. v0: always-invoke + hardcoded marketplace list +
@GoddamnAxl selection. Pure functions; SSE wiring lives in orchestrator."
```

---

### Task 2.3: Wire external pitch round + payment + SSE into orchestrator

**Files:**

- Modify: `agents/orchestrator.py`

This is where Producer becomes a real two-round orchestration:

1. Internal pitch round (`agent.pitching.* phase=internal`)
2. Producer external decision → marketplace → select → payment → external pitch round (`agent.pitching.* phase=external`)
3. Step 0.5 (memory) → Step 1 (guaranteed) → Step 1.5 (bonus) → Step 2 (script)

- [ ] **Step 1: Refactor `run_episode` to take a separate external-agent list**

Replace the signature in `agents/orchestrator.py:49-115`:

```python
def run_episode(
    internal_agents: list[DataAgent],
    external_agents: list[DataAgent] | None = None,
    user_id: str = "dev",
) -> tuple[dict[str, list[Pitch]], Brief]:
    """Run one episode generation pass — internal then external pitch round.

    The internal round runs first; Producer (in CLI/coordinator) decides
    to invoke external before the external round fires. This function
    runs both rounds and returns the merged pitch dict.

    Emits agent.pitching.* events with phase: "internal"|"external".

    Spec: docs/specs/2026-04-17-producer-alignment-plan.md Phase 2
          agents/docs/DESIGN.md §Reviewer Concern #2 (phase field)
    """
    from producer.events import emit

    external_agents = external_agents or []

    # ── Internal round ────────────────────────────────────────────────
    emit("agent.pitching.started", {"phase": "internal"})
    pitches_by_agent, brief = _run_pitch_round(internal_agents, user_id, phase="internal")
    emit("agent.pitching.done", {"phase": "internal"})

    # ── External round (if any) ───────────────────────────────────────
    if external_agents:
        emit("agent.pitching.started", {"phase": "external"})
        external_pitches, _ = _run_pitch_round(
            external_agents, user_id, phase="external", brief=brief,
        )
        emit("agent.pitching.done", {"phase": "external"})
        pitches_by_agent.update(external_pitches)

    return pitches_by_agent, brief


def _run_pitch_round(
    agents: list[DataAgent],
    user_id: str,
    phase: str,
    brief: Brief | None = None,
) -> tuple[dict[str, list[Pitch]], Brief]:
    """One round of fetch_context (parallel) → assemble Brief → pitch (parallel).

    If `brief` is provided (external round), skip Brief assembly and reuse it.
    """
    with concurrent.futures.ThreadPoolExecutor() as pool:
        ctx_futures = {a.name: pool.submit(a.fetch_context, user_id) for a in agents}
        mem_futures = {a.name: pool.submit(a.load_memory, user_id) for a in agents}
        contexts: dict[str, ScopeContext] = {n: f.result() for n, f in ctx_futures.items()}
        memories: dict[str, AgentMemory] = {n: f.result() for n, f in mem_futures.items()}

    if brief is None:
        # Internal round: assemble Brief from weather + calendar contexts.
        now = datetime.now()
        weather_summary: str | None = None
        calendar_events: list[str] | None = None
        for a in agents:
            ctx = contexts[a.name]
            if a.name == "weather":
                weather_summary = ctx.get("weather_summary")  # type: ignore[call-overload]
            elif a.name == "calendar":
                calendar_events = ctx.get("calendar_events")  # type: ignore[call-overload]
        today_context: TodayContext = {
            "date": now.date().isoformat(),
            "day_of_week": now.strftime("%A"),
            "time_of_day": _time_of_day(now.hour),
            "weather_summary": weather_summary,
            "calendar_events": calendar_events,
        }
        brief = {"today_context": today_context}

    agent_map = {a.name: a for a in agents}
    with concurrent.futures.ThreadPoolExecutor() as pool:
        pitch_futures = {
            name: pool.submit(
                agent_map[name].pitch, brief, memories[name], contexts[name], user_id
            )
            for name in contexts
        }
        pitches_by_agent: dict[str, list[Pitch]] = {
            name: f.result() for name, f in pitch_futures.items()
        }

    return pitches_by_agent, brief
```

- [ ] **Step 2: Update CLI to invoke external flow**

Replace the CLI `__main__` block (lines 128-217) to:

```python
if __name__ == "__main__":
    import argparse
    from producer.events import emit
    from producer.external import (
        decide_external_invocation,
        query_marketplace,
        select_external,
    )
    from payment.stub import initiate_tx

    parser = argparse.ArgumentParser(
        description="Run one episode generation pass and print EpisodeScript JSON."
    )
    parser.add_argument("--user-id", default="dev")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--no-external", action="store_true",
                        help="Skip external pitch round (Phase 2 escape hatch)")
    args = parser.parse_args()

    if args.no_llm:
        os.environ["DISABLE_LLM"] = "1"

    print(f"[orchestrator] Running episode for user_id={args.user_id!r} …\n")

    # ── Internal pitch round ─────────────────────────────────────────
    from agents.calendar.agent import CalendarAgent
    from agents.weather.agent import WeatherAgent
    from agents.youtube.agent import YouTubeAgent
    internal_agents = [WeatherAgent(), CalendarAgent(), YouTubeAgent()]

    pitches_by_agent, brief = run_episode(internal_agents, user_id=args.user_id)

    # ── Producer external decision → marketplace → payment → external pitch ──
    if not args.no_external:
        decision = decide_external_invocation(pitches_by_agent)
        emit("producer.external_decision.started", {
            "reason": "anti_cocoon_policy_v0",
            "reasoning_summary": decision["rationale"],
        })
        if decision["decision"] == "invoke":
            candidates = query_marketplace()
            emit("producer.marketplace.queried", {
                "candidates": [
                    {"handle": c["handle"], "display_name": c["display_name"],
                     "price_usdc": c["price_usdc"]}
                    for c in candidates
                ],
                "reasoning_summary": f"{len(candidates)} candidates available",
            })
            chosen = select_external(candidates, brief=brief)
            emit("producer.external_agent.selected", {
                "agent": chosen["handle"], "display_name": chosen["display_name"],
                "rationale": "expands topic diversity for this brief",
                "reasoning_summary": f"picked {chosen['handle']}",
            })

            # ── Agentic payment (mocked) ────────────────────────────
            tx = initiate_tx(
                from_wallet="0xPRODUCER",  # demo placeholder
                to_wallet=chosen["wallet_address"],
                amount_usdc=chosen["price_usdc"],
            )
            emit("payment.initiated", {
                "to": chosen["wallet_address"], "amount_usdc": chosen["price_usdc"],
                "mode_badge": tx["mode"],
            })
            emit("payment.confirmed", {
                "tx_hash": tx["tx_hash"],
                "basescan_url": tx["basescan_url"],
                "mode_badge": tx["mode"],
            })

            # ── External pitch round ────────────────────────────────
            from agents.alices.agent import AlicesAgent
            external_pitches, _ = run_episode(
                internal_agents=[],   # no second internal round
                external_agents=[AlicesAgent()],
                user_id=args.user_id,
            )
            pitches_by_agent.update(external_pitches)

    # ── Display per-agent pitches ────────────────────────────────────
    for agent_name, pitches in pitches_by_agent.items():
        print(f"── {agent_name} ({len(pitches)} pitch{'es' if len(pitches) != 1 else ''}) ──")
        for p in pitches:
            print(f"  [{p['priority']:.4f}] {p['title']}")

    # ── Step 0.5 + 1 + 1.5 + 2 (memory → guaranteed → bonus → script) ─
    from producer.memory import (
        apply_producer_memory, emit_memory_applied, load_producer_memory,
    )
    from producer.segments import select_guaranteed_slots, append_bonus
    from producer.bonus import select_bonus_with_events

    producer_memory = load_producer_memory(args.user_id)
    raw_pitches_by_agent = pitches_by_agent
    pitches_by_agent = apply_producer_memory(pitches_by_agent, producer_memory)
    emit_memory_applied(producer_memory, raw_pitches_by_agent, pitches_by_agent)

    order, remaining, bonus_budget = select_guaranteed_slots(pitches_by_agent)
    bonus, _ = select_bonus_with_events(
        guaranteed_slots=order["segments"],
        remaining_pitches=remaining,
        budget_remaining_sec=bonus_budget,
        today_context=brief["today_context"],
    )
    order = append_bonus(order, bonus)

    if args.no_llm:
        print(json.dumps(order, indent=2))
    else:
        from producer.script import generate_episode_script
        try:
            episode = generate_episode_script(order["segments"], brief)
            print(json.dumps(episode, indent=2))
        except Exception as e:
            print(f"[orchestrator] Producer LLM failed: {e}")
            print(json.dumps(order, indent=2))
```

- [ ] **Step 3: Add an integration test for the new wiring**

Modify `tests/test_orchestrator.py` (or `tests/test_agents_orchestrator.py`) to assert the external round runs separately and `agent.pitching.*` events carry `phase`. (Use stubbed agents to avoid OAuth.)

- [ ] **Step 4: Smoke-test the CLI**

```bash
DISABLE_LLM=1 python -m agents.orchestrator --no-llm
```

Expected: prints `producer.external_decision.started`, `producer.marketplace.queried`, `producer.external_agent.selected`, `payment.initiated`, `payment.confirmed`, then `producer.memory.applied` (only if non-bootstrap), then `producer.selecting.*`, then a JSON RunningOrder. AlicesAgent runs in the second pitch round.

- [ ] **Step 5: Commit**

```bash
git add agents/orchestrator.py tests/test_agents_orchestrator.py
git commit -m "feat(orchestrator): wire external pitch round + agentic payment

Decision 1d, Phase 2 finale. Producer now orchestrates: internal pitch
round → external decision → marketplace → mock payment → external pitch
round → memory → select → script. agent.pitching.* events carry phase."
```

---

### Task 2.4: Update `agents/docs/DESIGN.md` to mark `phase` field as implemented

**Files:**

- Modify: `agents/docs/DESIGN.md`

- [ ] **Step 1: Annotate Reviewer Concern #2 (line 209-214)**

Append a one-line resolution note: `**Resolved 2026-04-17:** `agent.pitching.{started,done}`carry`phase: "internal"|"external"` (see [`producer.events`](../../producer/events.py) wiring in `agents/orchestrator.py`).`

- [ ] **Step 2: Commit**

```bash
git add agents/docs/DESIGN.md
git commit -m "docs(agents): mark phase: internal|external SSE field implemented"
```

---

## Phase 3 — Per-segment streaming (decision 2a)

The largest refactor. Has a checkpoint at the top: decide whether to refactor `audio/orchestrator.py` to consume an `AsyncIterator` or build an intermediate streaming-coordinator.

### Task 3.0: Audio-boundary decision (CHECKPOINT — surface to user)

**Files:**

- Read: `audio/orchestrator.py`, `audio/docs/DESIGN.md`

- [ ] **Step 1: Read the full `audio/orchestrator.py` interface and the audio DESIGN section on segment dispatch**

```bash
cat audio/orchestrator.py audio/docs/DESIGN.md
```

- [ ] **Step 2: Choose between two integration shapes (BLOCK; ask user before proceeding)**

**Option A — Refactor `audio/generate_episode_audio` to accept `AsyncIterator[SegmentScript]`.**

- Cleanest end state.
- Changes audio component (per memory `feedback_component_by_component_dev.md`, ask user first).

**Option B — Build `producer/streaming_coordinator.py` that bridges Producer's iterator to audio's list.**

- Coordinator collects segment 0 from producer, immediately calls `audio.generate_episode_audio` for it, then collects 1-N as they arrive and re-invokes audio for the rest.
- Audio component untouched.
- Slight latency penalty: audio re-initializes per call; not a big deal at 4-6 segments.

**Output of this task:** ONE-LINE message to user: "Phase 3.0 checkpoint: pick A (refactor audio) or B (coordinator)."

- [ ] **Step 3: Wait for user choice before proceeding to Task 3.1**

Once chosen, the remaining Phase 3 tasks adopt that shape.

---

### Task 3.1: Refactor `producer/script.py` to async iterator

**Files:**

- Modify: `producer/script.py`
- Create: `tests/test_script_streaming.py`

This task is the same regardless of the 3.0 outcome — Producer's surface becomes async iterator either way.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_script_streaming.py`:

```python
"""Tests for producer/script.py async-iterator surface (Phase 3 / decision 2a).

Spec: producer/docs/DESIGN.md Reviewer Concern #1
      agents/docs/prompt_design.md §4 Step 2
"""
from __future__ import annotations

import asyncio

import pytest

from agents.protocol import Brief, Pitch
from producer.script import (
    SegmentScript,
    generate_segment,           # NEW: per-segment LLM call
    stream_episode_script,      # NEW: AsyncIterator[SegmentScript]
)


def _pitch(agent: str, title: str, seg_len: int = 90) -> Pitch:
    return {
        "agent": agent, "title": title, "hook": "h", "rationale": "r",
        "source_refs": [], "data": {}, "priority": 0.9,
        "thin_signal": False, "claim_kind": "neutral",
        "provenance_shape": "balanced", "suggested_length_sec": seg_len,
    }


def _brief() -> Brief:
    return {"today_context": {
        "date": "2026-04-17", "day_of_week": "Thursday",
        "time_of_day": "morning", "weather_summary": None,
        "calendar_events": None,
    }}


@pytest.mark.asyncio
async def test_stream_emits_segment_one_first(monkeypatch):
    """Decision 2a: segment 0 must arrive before segments 1-N start."""
    selected = [_pitch("youtube", "yt"), _pitch("calendar", "cal")]

    captured: list[str] = []

    async def fake_generate_segment(segment, brief, is_first):
        captured.append("call")
        return SegmentScript(
            agent=segment["agent"], pitch_title=segment["title"],
            segue_in="" if is_first else "And next…",
            script="x" * 50, estimated_length_sec=segment["suggested_length_sec"],
        )

    monkeypatch.setattr("producer.script.generate_segment", fake_generate_segment)

    received: list[SegmentScript] = []
    async for seg in stream_episode_script(selected, _brief()):
        received.append(seg)

    assert len(received) == 2
    assert received[0]["pitch_title"] == "yt"      # first input → first emitted
    assert received[0]["segue_in"] == ""           # first segment has no segue_in
    assert received[1]["segue_in"] != ""


@pytest.mark.asyncio
async def test_stream_validates_each_segment(monkeypatch):
    """Decision 2a: per-segment validation (script length floor) still applies."""
    selected = [_pitch("youtube", "yt")]

    async def too_short(segment, brief, is_first):
        return SegmentScript(
            agent=segment["agent"], pitch_title=segment["title"],
            segue_in="", script="hi", estimated_length_sec=10,
        )

    monkeypatch.setattr("producer.script.generate_segment", too_short)

    with pytest.raises(ValueError, match="too short"):
        async for _ in stream_episode_script(selected, _brief()):
            pass
```

- [ ] **Step 2: Run, confirm failures**

```bash
pytest tests/test_script_streaming.py -v
```

- [ ] **Step 3: Refactor `producer/script.py` — split into `generate_segment` + `stream_episode_script`**

Replace the file with:

````python
"""Producer LLM pass: per-segment streaming for P13.

Per-segment LLM call (`generate_segment`) + async iterator
(`stream_episode_script`). Segment 0 is critical-path; segments 1-N
fan out behind it.

Spec: producer/docs/DESIGN.md Reviewer Concern #1
      docs/specs/2026-04-17-producer-alignment-plan.md Phase 3 (decision 2a)

The legacy `generate_episode_script(selected, brief) -> EpisodeScript`
remains for back-compat (CLI fallback path); it consumes the iterator
internally.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import AsyncIterator, TypedDict

import anthropic

from agents.protocol import Brief, Pitch, TodayContext
from producer import DEFAULT_LLM_MODEL
from producer.events import emit
from producer.segments import TARGET_EPISODE_SECS

# ── Output types (unchanged) ─────────────────────────────────────────

class SegmentScript(TypedDict):
    agent: str
    pitch_title: str
    segue_in: str
    script: str
    estimated_length_sec: int


class EpisodeScript(TypedDict):
    cold_open: str
    segments: list[SegmentScript]
    sign_off: str


# ── Constants (unchanged) ────────────────────────────────────────────

MODEL = os.environ.get("PRODUCER_LLM_MODEL", DEFAULT_LLM_MODEL)
SEGMENT_MAX_TOKENS = 2048   # per-segment cap; full envelope was 8192
_MIN_SCRIPT_CHARS = 20

# (System prompt — keep the existing SYSTEM_PROMPT verbatim; full text in repo)
SYSTEM_PROMPT = """\
... (unchanged from current producer/script.py:46-178) ...
"""


# ── Per-segment LLM call ─────────────────────────────────────────────

async def generate_segment(
    segment: Pitch,
    brief: Brief,
    is_first: bool,
) -> SegmentScript:
    """Generate one SegmentScript via a single LLM call.

    First segment also produces the cold open's transition INTO it
    (segue_in stays empty per Step 2 spec; the cold open is generated
    separately via cold_open_for(...) in stream_episode_script).
    """
    if os.environ.get("DISABLE_LLM"):
        raise RuntimeError("LLM disabled via DISABLE_LLM env var")

    client = anthropic.Anthropic()
    payload = {
        "segment": {
            "agent": segment["agent"], "title": segment["title"],
            "hook": segment["hook"], "rationale": segment.get("rationale", ""),
            "source_refs": segment.get("source_refs", []),
            "data": segment.get("data", {}),
            "priority": segment["priority"],
            "claim_kind": segment.get("claim_kind", "neutral"),
            "provenance_shape": segment.get("provenance_shape", "balanced"),
            "thin_signal": segment.get("thin_signal", False),
            "suggested_length_sec": segment["suggested_length_sec"],
        },
        "today_context": dict(brief["today_context"]),
        "is_first": is_first,
        "target_total_secs": TARGET_EPISODE_SECS,
    }
    user_msg = json.dumps(payload, indent=2)

    # Run the sync Anthropic call in a thread so the async generator
    # doesn't block the event loop.
    response = await asyncio.to_thread(
        client.messages.create,
        model=MODEL, max_tokens=SEGMENT_MAX_TOKENS,
        system=SYSTEM_PROMPT, messages=[{"role": "user", "content": user_msg}],
        timeout=15.0,
    )
    if not response.content or response.content[0].type != "text":
        raise ValueError("LLM returned no text content")
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3].strip()
    data = json.loads(raw)

    seg = SegmentScript(
        agent=data["agent"],
        pitch_title=data["pitch_title"],
        segue_in="" if is_first else data.get("segue_in", ""),
        script=data["script"],
        estimated_length_sec=data.get("estimated_length_sec", 60),
    )

    if is_first and seg["segue_in"].strip():
        raise ValueError(
            f"First segment must have empty segue_in. Got: {seg['segue_in']!r}"
        )
    if len(seg["script"].strip()) < _MIN_SCRIPT_CHARS:
        raise ValueError(
            f"Segment ({seg['agent']}/{seg['pitch_title']}) script too short: "
            f"{len(seg['script'])} chars (min {_MIN_SCRIPT_CHARS})"
        )
    return seg


# ── Async iterator surface ───────────────────────────────────────────

async def stream_episode_script(
    selected: list[Pitch],
    brief: Brief,
) -> AsyncIterator[SegmentScript]:
    """Emit SegmentScripts one at a time. Segment 0 first (critical path).

    Spec: producer/docs/DESIGN.md Reviewer Concern #1.
    """
    # Validate input keys round-trip (cannot drop segments — same invariant
    # as the legacy monolithic path).
    input_keys = {(p["agent"], p["title"]) for p in selected}
    output_keys: set[tuple[str, str]] = set()

    for i, pitch in enumerate(selected):
        seg = await generate_segment(pitch, brief, is_first=(i == 0))
        output_keys.add((seg["agent"], seg["pitch_title"]))
        emit("script.segment.done", {
            "index": i, "agent": seg["agent"], "pitch_title": seg["pitch_title"],
        })
        yield seg

    missing = input_keys - output_keys
    if missing:
        raise ValueError(f"LLM dropped segment(s): {missing}")


# ── Back-compat sync surface (CLI keeps working) ─────────────────────

def generate_episode_script(selected: list[Pitch], brief: Brief) -> EpisodeScript:
    """Legacy: collect the iterator into a full EpisodeScript.

    Used by the CLI fallback path while the audio integration is wired.
    Once Phase 3 audio integration lands, the CLI calls stream_episode_script
    directly.
    """
    if os.environ.get("DISABLE_LLM"):
        raise RuntimeError("LLM disabled via DISABLE_LLM env var")

    async def _collect() -> EpisodeScript:
        segments: list[SegmentScript] = []
        async for s in stream_episode_script(selected, brief):
            segments.append(s)
        # Cold open + sign-off are short; re-use the last LLM call's spare
        # capacity, OR generate them as their own tiny calls. v0: emit a
        # placeholder; the full coordinator will own these.
        return EpisodeScript(
            cold_open="(cold open generated by coordinator in Phase 3.2)",
            segments=segments,
            sign_off="(sign-off generated by coordinator in Phase 3.2)",
        )

    return asyncio.run(_collect())
````

(Note: SYSTEM_PROMPT body needs to be copied verbatim from current script.py:46-178; abbreviated above.)

- [ ] **Step 4: Install pytest-asyncio if not already**

```bash
pip install pytest-asyncio
```

Add to `pyproject.toml` under `[tool.pytest.ini_options]`:

```toml
asyncio_mode = "auto"
```

- [ ] **Step 5: Run new + existing script tests**

```bash
pytest tests/test_script.py tests/test_script_streaming.py -v
```

Expected: streaming tests green; the legacy `tests/test_script.py` may need adjustment to point at `stream_episode_script` if its current cases assume monolithic.

- [ ] **Step 6: Commit**

```bash
git add producer/script.py tests/test_script_streaming.py pyproject.toml
git commit -m "feat(producer): per-segment streaming via stream_episode_script

Decision 2a, Phase 3 core. Splits monolithic generate_episode_script into
per-segment generate_segment() + AsyncIterator stream_episode_script().
Segment 0 is critical-path; legacy generate_episode_script() collects the
iterator for back-compat with the CLI fallback path."
```

---

### Task 3.2: Add cold open + sign-off as their own tiny LLM calls

The original monolithic call produced cold_open + segments + sign_off as one envelope. Per-segment streaming needs to source cold_open and sign_off separately.

**Files:**

- Modify: `producer/script.py`

- [ ] **Step 1: Add `generate_cold_open` and `generate_sign_off`**

Append to `producer/script.py`:

```python
async def generate_cold_open(
    selected: list[Pitch],
    brief: Brief,
) -> str:
    """LLM call: 10-15s spoken cold open including transition into segment 0.

    Tight prompt; <300 token output.
    """
    if os.environ.get("DISABLE_LLM"):
        raise RuntimeError("LLM disabled via DISABLE_LLM env var")
    client = anthropic.Anthropic()
    payload = {
        "first_segment": {
            "agent": selected[0]["agent"], "title": selected[0]["title"],
            "hook": selected[0]["hook"],
        },
        "today_context": dict(brief["today_context"]),
        "duration_sec_target": 12,
    }
    response = await asyncio.to_thread(
        client.messages.create,
        model=MODEL, max_tokens=400, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(
            {"task": "cold_open", **payload}, indent=2,
        )}], timeout=10.0,
    )
    return response.content[0].text.strip()


async def generate_sign_off(brief: Brief) -> str:
    """LLM call: ~10s spoken sign-off."""
    if os.environ.get("DISABLE_LLM"):
        raise RuntimeError("LLM disabled via DISABLE_LLM env var")
    client = anthropic.Anthropic()
    response = await asyncio.to_thread(
        client.messages.create,
        model=MODEL, max_tokens=200, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(
            {"task": "sign_off", "today_context": dict(brief["today_context"])},
            indent=2,
        )}], timeout=10.0,
    )
    return response.content[0].text.strip()
```

- [ ] **Step 2: Update `generate_episode_script` collector to use them**

Replace the placeholder cold_open/sign_off in the legacy `generate_episode_script`:

```python
async def _collect() -> EpisodeScript:
    cold_open = await generate_cold_open(selected, brief)
    segments: list[SegmentScript] = []
    async for s in stream_episode_script(selected, brief):
        segments.append(s)
    sign_off = await generate_sign_off(brief)
    return EpisodeScript(cold_open=cold_open, segments=segments, sign_off=sign_off)
```

- [ ] **Step 3: Smoke-test (this requires API access — skip if you don't have it locally)**

```bash
python -m agents.orchestrator
```

Expected: full episode script with real cold_open + sign_off.

- [ ] **Step 4: Commit**

```bash
git add producer/script.py
git commit -m "feat(producer): cold_open + sign_off as separate small LLM calls

Phase 3.2. Per-segment streaming needs cold_open and sign_off sourced
outside the main segment loop. Each is its own 200-400 token call."
```

---

### Task 3.3: Wire script.segment.\* SSE event to coordinator/audio (depends on 3.0 outcome)

**This task's exact shape depends on Task 3.0's checkpoint decision.** The plan template is:

- If Option A (refactor audio): refactor `audio/orchestrator.py` to accept `AsyncIterator[SegmentScript]`; the iterator is wired in `agents/orchestrator.py`'s CLI section.
- If Option B (coordinator): create `producer/streaming_coordinator.py` that bridges producer iterator → audio orchestrator.

In either case, the `script.segment.done` event from Task 3.1 already fires; this task just connects the consumer.

- [ ] **Step 1: After 3.0 returns from user, fill in this task**

Stop and ask the user to provide the implementation per their 3.0 choice; this plan does not pre-write the code so we don't bake in a wrong assumption.

---

### Task 3.4: Update `producer/docs/DESIGN.md` Reviewer Concern #1 to mark resolved

**Files:**

- Modify: `producer/docs/DESIGN.md`

- [ ] **Step 1: Annotate Reviewer Concern #1 (lines 75-84)**

Add a one-liner: `**Resolved 2026-04-17:** implemented as `stream_episode_script(...) -> AsyncIterator[SegmentScript]` in [`producer/script.py`](../script.py); per-segment LLM calls; first-segment critical-path emits `script.segment.done`SSE event for audio handoff. See`docs/specs/2026-04-17-producer-alignment-plan.md` Phase 3.`

- [ ] **Step 2: Commit**

```bash
git add producer/docs/DESIGN.md
git commit -m "docs(producer): mark Reviewer Concern #1 (P13 streaming) resolved"
```

---

## Self-review checklist

- [ ] Every of the 8 decisions has at least one task implementing it.
- [ ] Each task lists exact file paths and shows the actual code (no "implement function X" without code).
- [ ] Type names used in later tasks match earlier definitions (`RunningOrder`, `ExternalDecision`, `CreatorAgentListing` consistent throughout).
- [ ] Test commands include expected pass/fail outcome.
- [ ] Each phase ends in a working, testable, shippable state.
- [ ] No "TBD" / "TODO" / "fill in later" — Task 3.3 explicitly defers to user input from 3.0 instead.

---

## Decision recap (for reviewer convenience)

| #   | Discrepancy                     | Decision                                           | Phase   |
| --- | ------------------------------- | -------------------------------------------------- | ------- |
| 1   | External flow missing           | 1d (stub external + payment)                       | Phase 2 |
| 2   | `write_script` monolithic       | 2a (per-segment streaming)                         | Phase 3 |
| 3   | SSE events not emitted          | 3d (in-process bus + JSONL sink)                   | Phase 1 |
| 4   | Producer types undefined        | 4a (define in protocol.py + assemble RunningOrder) | Phase 0 |
| 5   | Tie-break non-deterministic     | 5a (`(-priority, agent, title)` sort key)          | Phase 0 |
| 6.1 | Stale model default             | 6.1a (hoist + bump to claude-sonnet-4-6)           | Phase 0 |
| 6.2 | Duplicate `TARGET_EPISODE_SECS` | 6.2b (import from segments.py)                     | Phase 0 |
| 6.3 | Duplicate `_segment_length`     | 6.3c (leave alone — divergence is intentional)     | n/a     |

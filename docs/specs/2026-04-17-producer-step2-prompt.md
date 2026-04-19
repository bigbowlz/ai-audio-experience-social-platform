# Producer Step 2 Prompt Redesign

**Status:** SUPERSEDED 2026-04-18 — see "Superseded by" note below.
**Original status:** APPROVED — brainstorming cleared 2026-04-17

> **Superseded by (2026-04-18):** This spec's core decision was to INCLUDE
> more fields in the Step-2 payload (`data`, `rationale`, `source_refs`).
> That decision was partially reversed on 2026-04-18 as part of the
> agent-output conventions pass:
>
> - `rationale` was removed from the `Pitch` TypedDict entirely (write-only
>   across the codebase).
> - `priority`, `suggested_length_sec`, `provenance_shape`, and
>   `target_total_secs` were removed from the Step-2 payload because
>   SYSTEM_PROMPT already told the LLM to ignore or not re-rank on them.
>   Keeping them in the payload was dead weight.
> - `data` and `source_refs` remain (this spec's Pitch-coverage fix still holds).
> - Weather, calendar, and alices hooks moved to a structured
>   WHAT / SOURCE / GOAL format; alices hooks explicitly flag external
>   curator provenance.
> - Per-agent provenance semantics (youtube=listener / alices=curator /
>   weather,calendar=context) are now encoded in SYSTEM_PROMPT rather
>   than as a new Pitch field.
>
> Canonical current state: `agents/docs/prompt_design.md` §4 Step 2,
> `producer/script.py` SYSTEM_PROMPT, and `agents/docs/DESIGN.md` §Pitch shape.
> The rest of this document documents the 2026-04-17 redesign as it landed;
> read it for the pacing / drop-segments / field-legend structure that is
> still in force.

**Parent docs:**

- `agents/docs/prompt_design.md` §4 Step 2 — current Step 2 spec (this redesign updates it)
- `agents/protocol.py` — `Pitch`, `Brief`, `TodayContext` shapes
- `producer/script.py` — current Step 2 implementation
- `producer/bonus.py` — Step 1.5 LLM prompt (style reference)
- `agents/youtube/llm.py` + `agents/youtube/docs/DESIGN.md` — claim_kind / provenance_shape directive reference
- `agents/weather/agent.py`, `agents/weather/docs/DESIGN.md` — weather `Pitch.data` shape
- `agents/calendar/agent.py`, `agents/calendar/docs/DESIGN.md` — calendar `Pitch.data` shape
- `learning_loop/docs/DESIGN.md` — `ProducerMemory` shape (in-flight)

**Memory feedback applied:**

- `feedback_producer_memory_deterministic.md` — ProducerMemory is applied as a pure function before any LLM pass, not injected as raw dict. Holds for Step 2 as well as Step 1.5.
- `feedback_component_by_component_dev.md` — only finalized components are binding; here, the work is internal to producer/script.py and agents are read-only references.

**Scope:** v0. Redesign of `producer/script.py:_format_input` and `SYSTEM_PROMPT`. Adds three structural validation checks. Does not respec Step 1 (`producer/segments.py`), Step 1.5 (`producer/bonus.py`), or any agent.

## Problem statement

Current `producer/script.py` underspecifies the Step 2 LLM contract on four axes:

1. **Pitch coverage gap.** `_format_input` passes 8 of ~11 Pitch fields. Missing: `data`, `rationale`, `source_refs`. The Producer LLM cannot use calendar events, weather hourly forecast / notable_facts, or each agent's rationale + source refs because those fields are dropped at the prompt boundary.
2. **Underspecified directives.** `SYSTEM_PROMPT` names `claim_kind` and `provenance_shape` but never defines their enum values or the behavior they should induce. Same omission for `thin_signal`, `priority`, the hook-vs-rationale-vs-data layering, and per-agent data schemas.
3. **No field-by-field legend.** The LLM is inferring field semantics from field names alone.
4. **Inconsistent enforcement.** Only one structural assertion (cannot drop segments) protects an `EpisodeScript`. The first-segment-no-segue rule and minimum-script-length are stated as soft guidance but never enforced.

## Locked invariants (do not break)

| Invariant                                         | Source                                                                              |
| ------------------------------------------------- | ----------------------------------------------------------------------------------- |
| Cannot drop segments                              | prompt_design.md §4 Step 2 constraint #1; producer/script.py:171-178                |
| Cannot invent segments                            | prompt_design.md §4 Step 2 constraint #2                                            |
| Memory isolation (P9): Producer is memory-blind   | learning_loop/docs/DESIGN.md; feedback_producer_memory_deterministic.md             |
| Agent hooks are creative briefs, not scripts      | prompt_design.md §3                                                                 |
| Hook ownership: Producer never rewrites hooks     | prompt_design.md §3 ("Producer rewrites agent hooks" rejected alternative)          |
| Two-LLM boundary: agents own taste, Producer owns world | agents/docs/DESIGN.md §Two-LLM boundary                                       |

## Decisions

### D1 — Payload schema: verbatim Pitch fields, no projection

`_format_input` passes every Pitch field per segment, with no per-agent collapsing. Defaults applied for missing optional fields:

```python
{
    "selected_segments": [
        {
            "agent": str,
            "title": str,
            "hook": str,
            "rationale": str,                   # default ""
            "source_refs": list[str],           # default []
            "data": dict,                       # default {}
            "priority": float,
            "claim_kind": str,                  # default "neutral"
            "provenance_shape": str,            # default "balanced"
            "thin_signal": bool,                # default False
            "suggested_length_sec": int,
        },
        # ...
    ],
    "today_context": TodayContext,
    "target_total_secs": int,                   # 450 for v0
}
```

**No `producer_memory` in payload.** Per `feedback_producer_memory_deterministic.md`, ProducerMemory is applied as a pure function upstream — agent-weight scaling happens at Step 1 / Step 1.5; opener-preference reordering happens before Step 2 sees `selected_segments`. Step 2 LLM operates on already-nudged inputs.

**No per-agent data projection.** Weather's full `Pitch.data` (including 24-entry `hourly_forecast`) is passed verbatim. Decision deferred to v1 — see TODOS.md "Weather data digest before Producer prompt."

**Pure JSON user message, no preamble.** All semantics live in the system prompt (cached); user message stays machine-friendly and per-episode.

### D2 — System prompt structure: 8 blocks

Block 1 — **Role.** Same as today. "You are a radio show producer …".

Block 2 — **Hard rules.** Six existing rules + new rule: "First segment's `segue_in` is empty — the cold open includes the transition into it."

Block 3 — **Field legend.** One line per Pitch field naming what it is and what behavior it constrains:

- `agent` — source agent; informs ordering heuristics.
- `title` — short label; must round-trip verbatim in `pitch_title`.
- `hook` — creative brief from the agent. Not spoken verbatim. For taste agents, caps the phrasing you may use (see Block 6). For context agents, a one-line summary of the data.
- `rationale` — why the agent selected this topic. Context for tone; never spoken.
- `source_refs` — channel names / video titles (human-readable, NOT IDs). Reference sparingly where natural ("a channel you've been subscribed to," "a video you liked"). Do not recite the full list.
- `data` — structured payload from the agent. Per-agent crib in Block 5.
- `priority`, `suggested_length_sec` — scheduling metadata, not script-level knobs.
- `claim_kind` — temporal framing permission. See Block 4.
- `provenance_shape` — evidence framing permission. Already enforced by the agent in the hook; informational here. See Block 6.
- `thin_signal` — when `true`, the agent had insufficient personalization data. See Block 7.

Block 4 — **`claim_kind` directive table.** Mirrors `agents/youtube/llm.py` lines 56–65. Four rows (durable / rising / discovery / neutral) listing permitted vs. prohibited temporal phrasing. Producer does not invent temporal claims beyond what the hook made.

Block 5 — **Per-agent `data` crib.** Tight paragraph per agent:

- **weather** — `data.current` (temp/condition/wind), `data.day_ahead` (upcoming high/low/sunset), `data.notable_facts` (top 3 ranked radio-interesting facts), `data.air_quality`, `data.location_name`. Ignore `hourly_forecast` and `day_past` unless surfacing a specific hour matters.
- **calendar** — `data.api_reachable` (bool), `data.events[]` with `summary`, `start`, `end`, `duration_min`, `attendee_count`, `attendees` (list of display names; may be shorter than `attendee_count` or empty when names aren't resolvable — never contains emails), `is_recurring`, `has_video_call`, `organizer`.
- **youtube** / **alices** — `data` is usually `{}`. Hook + rationale + source_refs are the substrate.

Block 6 — **Hook vs. data layering rule** (the central invariant):

> For taste agents (`youtube`, `alices`): the hook is the phrasing ceiling. `claim_kind` and `provenance_shape` bound what you may claim; `data` is read-only context for tone calibration only. Do not combine facts from `data` into new temporal or intensity claims the hook did not make.
>
> For context agents (`weather`, `calendar`): `data` is the content source. `hook` is a one-line safety net / summary. Prefer `data` when writing the segment body; use `hook` only as a fallback framing.
>
> `provenance_shape` is already enforced by the agent when writing the hook. You do not need a shape table. Do not invent references to subscriptions, channels, or likes beyond what the hook already cites.

Block 7 — **`thin_signal` handling.** Per-agent factual close, no action prompts:

> When `thin_signal: true`, write a general-interest segment in the agent's domain — no personalization, no channels/subs/events by name. Optionally close with one factual sentence:
>
> - **youtube** / **alices** — "This will get more personal as your YouTube activity grows." (cause: sparse subs/likes — not actionable in the short term)
> - **weather** — "Local forecast wasn't available today." (cause: location skipped at generation, or forecast API failure — both opaque from the script's POV)
> - **calendar** — N/A. Calendar never emits `thin_signal` in v0 (per `agents/calendar/docs/DESIGN.md` §Protocol Compliance).
>
> Keep the line factual and brief. If awkward, omit it. Never recite reasons across multiple segments — one per `thin_signal` segment, in that segment's own script.

Block 8 — **Voice + output format.** Same as today: warm/conversational voice + JSON schema + "Return ONLY the JSON — no fences."

### D3 — Validation: three structural checks

Added to `generate_episode_script()` post-parse, in this order, all `raise ValueError`:

1. **Existing.** Agent/title round-trip — input keys ⊆ output keys (cannot-drop-segments invariant).
2. **New.** `segments[0].segue_in.strip() == ""` (first-segment rule, currently soft).
3. **New.** Every `SegmentScript.script.strip()` length ≥ `_MIN_SCRIPT_CHARS` (= 20). Threshold handles "Mostly cloudy, 14C." floor; anything shorter is parse artifact or broken response.

Caller (orchestrator) decides retry/fallback. Producer module's job is to refuse to return a malformed `EpisodeScript`.

**Not added** (rejected — see Rejected Alternatives):

- Regex temporal-word check.
- Length-estimate sanity check.

### D4 — Test plan

New file: `tests/test_script.py`. ~13 cases in 4 groups. Mocking via monkeypatch on `anthropic.Anthropic` — one helper `_mock_llm(monkeypatch, response_dict)` returns a stub client whose `messages.create` returns canned responses.

**Group A — payload shape (no LLM, asserts `_format_input` output):**

1. `test_format_input_passes_all_pitch_fields` — fully-populated Pitch → assert each segment dict contains all 11 fields.
2. `test_format_input_includes_today_context_and_target` — top-level keys are exactly `selected_segments`, `today_context`, `target_total_secs` (no `producer_memory`).
3. `test_format_input_defaults_missing_optional_fields` — minimal Pitch → defaults `rationale=""`, `source_refs=[]`, `data={}`, `claim_kind="neutral"`, `provenance_shape="balanced"`, `thin_signal=False`.
4. `test_format_input_preserves_data_verbatim` — weather Pitch with full `hourly_forecast` → `data` round-trips byte-identical.

**Group B — system prompt structural assertions:**

5. `test_system_prompt_has_claim_kind_directive_block` — all 4 claim_kind values + words `permitted`/`prohibited` appear.
6. `test_system_prompt_has_field_legend` — each Pitch field name appears (`hook`, `rationale`, `source_refs`, `data`, `claim_kind`, `provenance_shape`, `thin_signal`, `priority`, `suggested_length_sec`).
7. `test_system_prompt_has_per_agent_data_crib` — `weather`, `calendar`, `youtube`, `alices` each appear in a data context (substring match for `data.current`, `data.events`, etc.).
8. `test_system_prompt_has_thin_signal_handling` — SYSTEM_PROMPT mentions `thin_signal` and includes per-agent nudge phrasings.
9. `test_system_prompt_has_hook_data_layering_rule` — phrases `phrasing ceiling`, `read-only context`, `content source`.

**Group C — validation assertions (mocked LLM response, asserts `ValueError`):**

10. `test_drops_segment_raises` — formalize existing behavior; mock LLM missing one segment; assert message names agent.
11. `test_first_segment_nonempty_segue_in_raises` — mock `segments[0].segue_in = "And now..."`; assert message mentions `segue_in`.
12. `test_short_script_raises` — mock segment with `script = "Hi."`; assert message mentions char count + segment identity.

**Group D — happy path:**

13. `test_well_formed_response_passes` — mock LLM with valid 2-segment EpisodeScript; assert returns `EpisodeScript` with correct shape; no exceptions.

Out of scope: `tests/test_segments.py` (Step 1) and `tests/test_bonus_selection.py` (Step 1.5) untouched.

### D5 — Documentation updates

1. **`agents/docs/prompt_design.md` §4 Step 2 (lines ~540–600).**
   - Update **Input** code block: add per-segment `rationale`, `source_refs`, `data`. Remove top-level `producer_memory`.
   - Update **System prompt constraints** list: add new constraints #7 (first-segue-empty), #8 (per-agent data crib), #9 (hook vs. data layering), #10 (thin_signal per-agent nudges).
   - Update **Producer memory** subsection: change "Producer reads `producer_memory` at script-time" to "Producer memory is applied deterministically upstream (see `feedback_producer_memory_deterministic.md`). Step 2 LLM does not receive producer_memory."
   - Update **Test mandate** table: add row for Step 2 validation tests (drop / first-segue-empty / short-script).

2. **`TODOS.md` — new section `## Producer (from /brainstorming 2026-04-17)`** with two entries:
   - **Weather data digest before Producer prompt** (Medium, v1) — project weather `Pitch.data` to lean subset.
   - **Web search for thin_signal segments** (Low, v1, alongside "what's new" feed).

## Rejected alternatives

| Alternative                                                                               | Why rejected                                                                                                                                                                                                                                                                                |
| ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Pass `producer_memory` dict into Step 2 prompt                                            | Violates `feedback_producer_memory_deterministic.md`. ProducerMemory is applied as a pure function upstream (priority scaling, opener reordering); LLM sees the result, not the dict. Same rule that applies to Step 1.5 applies here.                                                      |
| Per-agent `data` projection in Producer's `_format_input` (e.g., drop weather hourly)     | Agreed-correct for v1; deferred for v0 (TODOS.md). Picked v0 simplicity (verbatim pass) over v0 token savings.                                                                                                                                                                              |
| Agent-exposed `data_for_producer()` helper                                                | Adds protocol method, couples agent internals to Producer consumption concerns. Heavy for what it buys. Revisit only if marketplace agents arrive with token-heavy `data`.                                                                                                                  |
| Branched system prompt per agent                                                          | Scales poorly (4 agents → 4 branches; marketplace agents → N branches). Field legend + per-agent data crib in one prompt is sufficient.                                                                                                                                                     |
| Short preamble in user message ("Episode brief follows. See system prompt…")              | <30 tokens, but does not cache. Pure JSON pays system-prompt tokens once; preamble repeats per call. Pure JSON wins.                                                                                                                                                                        |
| `provenance_shape` directive table in Producer prompt                                     | Redundant. Agent's LLM already enforces `provenance_shape` when writing the hook. Producer operating on the hook would re-apply a constraint already respected. Belt-and-suspenders. Replaced with one folded sentence in Block 7.                                                          |
| Regex temporal-word check on taste-agent `claim_kind=neutral` segments                    | False-positive risk (legit hooks may quote channel names containing forbidden words). Duplicates work the agent's LLM already did at the hook layer. Same critique as `provenance_shape` table.                                                                                             |
| Length-estimate sanity check (`sum(estimated_length_sec)` vs. `target_total_secs ± 30%`) | Useful for observability (could be an SSE event), not correctness. Not a gate.                                                                                                                                                                                                              |
| Action prompts in `thin_signal` nudges ("sign in next time," "connect Calendar next time") | OAuth happens at generation time, not sign-in time. youtube `thin_signal` isn't an auth issue at all — it means OAuth succeeded but user has sparse activity. No re-auth would fix it. Action prompts would be inaccurate or condescending. Replaced with factual, optional close.          |
| Web search inside Producer for `thin_signal` (especially youtube)                         | Conflicts with prompt_design.md §3 ("v0 does not build a 'what's new' feed"). New tool-use path, new hallucination surface at the Producer layer (the layer the two-LLM boundary protects), latency hit, scope creep. Defer to v1 alongside the planned "what's new" feed. (TODOS.md)       |
| Calendar `thin_signal` handling block                                                     | Calendar never emits `thin_signal` in v0 (`agents/calendar/docs/DESIGN.md` §Protocol Compliance). Including a calendar nudge would be dead code in the prompt.                                                                                                                              |

## Token budget check

Worst-case 4-agent episode (1 youtube + 1 weather + 1 calendar + 1 alices, 2 bonus slots):

- System prompt: ~1.8k tokens (Blocks 1–9 with directive tables, field legend, per-agent crib, hook/data layering, thin_signal handling, voice + output format). Cached.
- Per-segment payload (typical):
  - youtube/alices: ~400 tokens (hook + rationale + source_refs[~5 names])
  - weather (verbatim): ~3k tokens (full `hourly_forecast` 24×~10 fields, current, day_past, day_ahead, air_quality, notable_facts, location_name) — dominant cost
  - calendar: ~600 tokens (events[~5 typical] × 8 fields)
- 6-segment payload: ~5k tokens
- today_context: ~80 tokens

Total per-call: ~7k tokens (1.8k cached + 5k payload + ~100 misc). Within Claude Sonnet limits, well below `MAX_TOKENS=8192` output cap. Weather is 60% of payload — confirms the v1 digest TODO is real, but acceptable for v0.

## Test mandate

The three new structural validations in D3 + the prompt-content assertions in D4 Group B must land before Step 2 generates against real data. Group A (payload shape) and Group C (validation) are required for merge; Group B (prompt content) and D (happy path) catch regressions when the prompt is later edited.

## Open questions (parked)

- **Producer memory shape for opener-preference reordering.** The deterministic application of ProducerMemory.opener_agent_preference (sorting `selected_segments` to put the preferred agent first before Step 2) lives outside this spec. Belongs to learning-loop session or Step 1.5 redesign.
- **Marketplace agent `data` shapes.** Per-agent data crib in Block 6 covers v0 agents only. When marketplace agents arrive, either the crib needs an extension mechanism or marketplace agents publish their data shape in metadata. Defer to marketplace design session.
- **Length-estimate observability.** D3 explicitly excludes a length-estimate sanity check from validation. If observability of LLM under/over-allocation is wanted, surface as an SSE event from `generate_episode_script()`, not as a validation gate.

## Dependencies

| Component                  | Contract from this spec                                                                | Direction                  |
| -------------------------- | -------------------------------------------------------------------------------------- | -------------------------- |
| `producer/script.py`       | New `_format_input` payload, expanded SYSTEM_PROMPT, three validation checks           | this spec implements       |
| `agents/docs/prompt_design.md` | §4 Step 2 updated to match final schema                                            | this spec updates          |
| `TODOS.md`                 | Two new entries: weather data digest (v1), web search for thin_signal (v1)             | this spec adds             |
| `tests/test_script.py`     | New file, ~13 cases in 4 groups                                                        | this spec creates          |
| `producer/bonus.py`        | Unchanged. Style reference only.                                                       | unchanged                  |
| `producer/segments.py`     | Unchanged.                                                                             | unchanged                  |
| `agents/protocol.py`       | Unchanged. Pitch shape stays.                                                          | unchanged                  |
| `agents/{youtube,weather,calendar,alices}/agent.py` | Unchanged. Producer reads what they emit.                           | unchanged                  |
| Learning-loop ProducerMemory redesign (in flight) | Step 2 prompt does not consume `producer_memory`. Upstream pure-function application is the integration point. | forward reference          |

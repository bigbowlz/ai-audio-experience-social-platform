# TODOS

Deferred work tracked from design reviews. Each item includes context so someone picking it up in 3 months understands motivation.

## v0 (CLI)

### Producer — Weather data digest before Producer prompt (from /brainstorming 2026-04-17)

**Status:** Partially resolved 2026-04-18 — `hourly_forecast` was dropped
at the AGENT layer (`agents/weather/agent.py`) rather than via a
Producer-side projection, which is simpler and avoids per-agent coupling
in the Producer. `day_past` is still in `Pitch.data` but SYSTEM_PROMPT
instructs the LLM to ignore it unless a specific hour matters. Remaining
work is deciding whether to drop `day_past` structurally too; tracked as
a follow-up below if it matters for token cost.
**Priority:** Low (was medium — the dominant token cost was
`hourly_forecast`, now gone)
**What:** Producer's `_format_input` projects weather `Pitch.data` to a lean subset (`current`, `day_ahead`, `notable_facts`, `air_quality`, `location_name`) instead of passing the full 24-entry `hourly_forecast`.
**Why:** v0 passes weather data verbatim — ~3-4k tokens per episode just on weather, most unused by a 45s segment. Cutting `hourly_forecast` and `day_past` saves ~60% of weather tokens with no semantic loss for typical episodes.
**Pros:** Smaller prompts, faster generation, lower cost.
**Cons:** Per-agent projection logic in Producer creates coupling. If marketplace agents arrive with token-heavy `data`, this becomes a 1-by-1 patch rather than a general solution. Consider whether to formalize with an agent-side `data_for_producer()` helper at that point.
**Depends on:** Nothing. Drop-in change to `producer/script.py:_format_input`.
**Context:** Brainstorming session 2026-04-17 picked verbatim pass-through (option A) for v0 simplicity. Q1 in that session.


## v1 (deferred)

> Items below require a frontend (web player, selection UI, or browser-based audio) and are explicitly out of scope for v0. v0 is CLI-only; see `README.md` for the CLI usage block.

- Webpage for agent selection (moves today's CLI flags into a UI)
- Web player for the produced audio
- Real-time like/skip/repeat via browser UI (replaces v0 CLI hotkeys)

### Audio component — Tab backgrounding / visibilitychange handler (from /plan-eng-review 2026-04-16)

**Priority:** Medium
**What:** Add `visibilitychange` listener to resume `AudioContext` on tab refocus and reconcile segment queue state.
**Why:** Browser suspends AudioContext when tab loses focus. During multi-second generation wait, user switching tabs silently breaks all audio playback. MusicFiller state machine desyncs, segments pile up unplayed.
**Pros:** Prevents the most common UX failure path (user switches tabs while waiting).
**Cons:** ~20 lines, touches Player component (not yet designed).
**Depends on:** Player component design.
**Context:** Outside voice finding from eng review. The fix is `document.addEventListener('visibilitychange', () => { if (!document.hidden) audioContext.resume(); })` plus queue reconciliation.

### Audio component — MP3 silence trimming for segment transitions (from /plan-eng-review 2026-04-16)

**Priority:** Low (evaluate during Day 5 rehearsal)
**What:** Post-TTS `ffmpeg -af silenceremove` step to trim leading/trailing silence from each segment MP3.
**Why:** ElevenLabs MP3 output has variable encoder padding. The 50ms crossfade mitigates but may not fully mask pops or doubled silence at segment boundaries.
**Pros:** Professional-sounding transitions, especially on headphones.
**Cons:** Adds ffmpeg as a runtime dependency (currently offline-only), ~50ms latency per segment.
**Depends on:** Day 5 rehearsal listen-through — only implement if transitions are audibly distracting.
**Context:** Outside voice finding. Evaluate empirically before building. The crossfade may be sufficient.

### Audio component — api-storage file-to-URL mapping contract (from /plan-eng-review 2026-04-16)

**Priority:** High (blocks Day 3 integration)
**What:** Document the contract: audio writes to `./data/episodes/{episode_id}/segment_{n}.mp3`, api-storage serves at `/audio/{episode_id}/segment_{n}.mp3`. api-storage must implement a static-file route mapping `./data/episodes/` to `/audio/`.
**Why:** TTSClient returns URLs but doesn't serve them. api-storage serves them but doesn't know where files are. The mapping is implicit and will cause confusion at integration time.
**Pros:** Clean integration contract between audio and api-storage.
**Cons:** None. One paragraph.
**Depends on:** api-storage component design session.
**Context:** Outside voice finding. The design mentions the route in the dependencies table but doesn't specify the disk-to-URL mapping rule.

### Calendar agent — Calendar as tempo signal ("invisible director") (from /plan-eng-review 2026-04-16)

**Priority:** Medium (v1)
**What:** Calendar agent emits a pacing profile alongside its pitches: `{pace: "compressed"|"relaxed"|"mixed", max_segment_sec: int}`. Producer's `select_segments()` consumes this signal to adjust segment lengths and episode pacing dynamically.
**Why:** Right now, calendar pitches a segment about your day. With a tempo signal, calendar shapes the *feel* of the entire episode. Busy day with 7 meetings → shorter, punchier segments across all agents. Light day → longer deep-dives, room to breathe. The listener doesn't hear "your day is busy" as a segment, they feel it in the pacing of everything else.
**Pros:** Calendar becomes the "invisible director" of the show. Most differentiated demo moment — no other podcast app does tempo-aware pacing. Outside voice from design review called this "the coolest version not considered."
**Cons:** ~20 lines in `select_segments()` to consume the signal, plus calendar emitting it. Requires a design decision about how much control calendar should have over other agents' segment lengths.
**Depends on:** Calendar agent v0 (live API + LLM pitch) shipping first.
**Context:** Design doc Approach C. The signal shape would be a new field in ScopeContext or a separate return value from pitch(). `select_segments()` currently has hardcoded `DEFAULT_SEGMENT_SEC` per agent — the tempo signal would dynamically adjust these based on calendar density.

### Producer — Web search for thin_signal segments (from /brainstorming 2026-04-17)

**Priority:** Low (v1, alongside the "what's new" feed)
**What:** When a segment has `thin_signal: true` (especially youtube/alices), Producer LLM does a web search for trending content in a curated topic list (Music/Tech/Gaming/etc.) and uses results to ground the general-interest segment.
**Why:** Thin-signal segments fall back to LLM priors today. Grounding in real trending content makes them more topical and useful.
**Pros:** Better thin-signal experience. Aligns with prompt_design.md §3 "content discovery is Producer's job."
**Cons:** New tool-use path (Claude web-search), new hallucination surface at the Producer layer (the layer the two-LLM boundary was designed to keep clean), latency hit (3-10s per search), and scope creep — once youtube has it, weather/calendar thin_signal cases will want it too.
**Depends on:** prompt_design.md §Open Questions "Producer's 'what's new' feed (v1+)" — these should be designed together as one v1 feature.
**Context:** Brainstorming session 2026-04-17. Design intent is to defer content discovery to a dedicated v1 design session, not add it ad-hoc inside Step 2.

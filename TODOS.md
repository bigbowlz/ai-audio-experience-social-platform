# TODOS

Deferred work tracked from design reviews. Each item includes context so someone picking it up in 3 months understands motivation.

## Audio component (from /plan-eng-review 2026-04-16)

### Tab backgrounding / visibilitychange handler
**Priority:** Medium
**What:** Add `visibilitychange` listener to resume `AudioContext` on tab refocus and reconcile segment queue state.
**Why:** Browser suspends AudioContext when tab loses focus. During multi-second generation wait, user switching tabs silently breaks all audio playback. MusicFiller state machine desyncs, segments pile up unplayed.
**Pros:** Prevents the most common UX failure path (user switches tabs while waiting).
**Cons:** ~20 lines, touches Player component (not yet designed).
**Depends on:** Player component design.
**Context:** Outside voice finding from eng review. The fix is `document.addEventListener('visibilitychange', () => { if (!document.hidden) audioContext.resume(); })` plus queue reconciliation.

### MP3 silence trimming for segment transitions
**Priority:** Low (evaluate during Day 5 rehearsal)
**What:** Post-TTS `ffmpeg -af silenceremove` step to trim leading/trailing silence from each segment MP3.
**Why:** ElevenLabs MP3 output has variable encoder padding. The 50ms crossfade mitigates but may not fully mask pops or doubled silence at segment boundaries.
**Pros:** Professional-sounding transitions, especially on headphones.
**Cons:** Adds ffmpeg as a runtime dependency (currently offline-only), ~50ms latency per segment.
**Depends on:** Day 5 rehearsal listen-through — only implement if transitions are audibly distracting.
**Context:** Outside voice finding. Evaluate empirically before building. The crossfade may be sufficient.

### api-storage file-to-URL mapping contract
**Priority:** High (blocks Day 3 integration)
**What:** Document the contract: audio writes to `./data/episodes/{episode_id}/segment_{n}.mp3`, api-storage serves at `/audio/{episode_id}/segment_{n}.mp3`. api-storage must implement a static-file route mapping `./data/episodes/` to `/audio/`.
**Why:** TTSClient returns URLs but doesn't serve them. api-storage serves them but doesn't know where files are. The mapping is implicit and will cause confusion at integration time.
**Pros:** Clean integration contract between audio and api-storage.
**Cons:** None. One paragraph.
**Depends on:** api-storage component design session.
**Context:** Outside voice finding. The design mentions the route in the dependencies table but doesn't specify the disk-to-URL mapping rule.


## Calendar agent (from /plan-eng-review 2026-04-16)

### Calendar as tempo signal ("invisible director")
**Priority:** Medium (v1)
**What:** Calendar agent emits a pacing profile alongside its pitches: `{pace: "compressed"|"relaxed"|"mixed", max_segment_sec: int}`. Producer's `select_segments()` consumes this signal to adjust segment lengths and episode pacing dynamically.
**Why:** Right now, calendar pitches a segment about your day. With a tempo signal, calendar shapes the *feel* of the entire episode. Busy day with 7 meetings → shorter, punchier segments across all agents. Light day → longer deep-dives, room to breathe. The listener doesn't hear "your day is busy" as a segment, they feel it in the pacing of everything else.
**Pros:** Calendar becomes the "invisible director" of the show. Most differentiated demo moment — no other podcast app does tempo-aware pacing. Outside voice from design review called this "the coolest version not considered."
**Cons:** ~20 lines in `select_segments()` to consume the signal, plus calendar emitting it. Requires a design decision about how much control calendar should have over other agents' segment lengths.
**Depends on:** Calendar agent v0 (live API + LLM pitch) shipping first.
**Context:** Design doc Approach C. The signal shape would be a new field in ScopeContext or a separate return value from pitch(). `select_segments()` currently has hardcoded `DEFAULT_SEGMENT_SEC` per agent — the tempo signal would dynamically adjust these based on calendar density.

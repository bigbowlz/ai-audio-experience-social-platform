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

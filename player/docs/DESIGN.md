# Component: `player`

**Status:** DRAFT (component extract from master design)
**Master doc:** [`~/.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md`](../../../../.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md) — canonical source.
**Reviewed:** 2026-04-13 (spec review 6/10, red-team)

## Purpose

The web-UI player card + the generation view. Two jobs:

1. **Playback** — Huxe-style card (P11), gapless queue over per-segment MP3s (P13-consumer), music filler during orchestration + backpressure (P12-consumer)
2. **Telemetry surface** — every control press emits a `/react` event; the player IS the training signal surface (P11)

Secondary: the generation view (pitch-round trace, producer decisions, payment beat, memory-update panel) subscribes to SSE and animates each event.

## Key premises

- **P11** Huxe-style card; player IS the telemetry surface
- **P12 (consumer)** Music filler plays during orchestration + backpressure gaps
- **P13 (consumer)** Gapless queue over streaming per-segment MP3s

## Interface contract

```typescript
// Core card controls (all emit /react POSTs)
type ReactEvent = {
  type: "skip" | "replay" | "more_like";
  episode_id: string;
  segment_index: number;
  segment_agent: string;
  segment_position_sec: number;  // captured BEFORE any playback mutation
  timestamp_ms: number;
};

// Gapless queue (P13)
interface SegmentQueue {
  enqueue(segment_index: number, url: string): void;   // e.g. "/audio/{episode_id}/segment_{n}.mp3"
  onSegmentDone(cb: (idx: number) => void): void;   // drives /react emission
  onUnderrun(cb: () => void): void;                 // drives music filler fade-in
}

// Single shared AudioContext (see Reviewer Concern #2)
const audioContext = new AudioContext();  // ONE, shared across all <audio> elements
```

## UI layout (master spec)

```
┌──────────────────────────────────────────────────────┐
│  Episode A · 21:04                                   │
│       [ cover art / segment-color stripe ]           │
│                                                      │
│  ──────●─────────────────────────────  04:12 / 21:04 │
│  [cal][youtube][@AlicesLens][youtube][weather]     │ ← segment track
│                                                      │
│     ⟲ 15s       ▶ / ∥       ⏭ skip segment          │
│                                                      │
│   Now: @AlicesLens — "indie devs vs. scale"        │
│   Next: @ofmiles — 32-min on rest ethic              │
│                                                      │
│   [⋯ long-press current card → "more like this"]     │
│                                                      │
│   [ End session ] ← see Reviewer Concern #5          │
└──────────────────────────────────────────────────────┘
```

## Dependencies on other components

| Component | Contract | Direction |
|---|---|---|
| `audio` | produces per-segment MP3s on local disk; player loads them via `/audio/:episode_id/:segment_n` (range-request-enabled). MusicFiller class. | in |
| `api-storage` | POSTs to `/react`; subscribes to SSE stream from `/generate` | out |
| `learning-loop` | emits `EpisodeSignals`-shape events | out |

## Build plan touchpoints

- **Day 3:** Pitch-round view (SSE subscriber, animates each event). Basic player: `<audio>` element + overlay card + ⟲-15s + ⏭ skip. Music filler class. Single shared AudioContext unlock (see Reviewer Concern #2). POST `/react` on skip/replay. See Reviewer Concern #4 for Day-3 scope cut.
- **Day 4 morning:** P13 client-queue + gapless transitions (moved from Day 3 per audio/docs). Long-press `more_like` detector (400ms).
- **Day 4 afternoon:** Memory Update panel rendering (Approach B gate).
- **Day 6 rehearsal:** Log which audio elements actually played during rehearsal to catch silent autoplay failures before Monday.

## Success criteria

- Day 3: pitch-round events animate on screen as SSE arrives; basic player plays a cached episode; ⟲ and ⏭ fire real `/react` events
- Day 4: gapless segment-to-segment transition with no audible pops; long-press emits `more_like`
- Day 6: 2 dress rehearsals show zero dead-air events, zero autoplay-rejected music-filler starts
- Demo: all 3 signals (skip, replay, more_like) emit with correct `segment_position_sec` captured at the right moment

## Reviewer concerns

### 1. Browser autoplay unlock fragility (severity: CRITICAL) — B-3

Master specifies "0.1-sec silent sample on Generate button" unlocks AudioContext. Under real conditions (Zoom grabbing audio focus, React hydration race, per-element AudioContext) this may silently fail. Result: 25-40 sec of dead air in Minute 1-3 while the reasoning trace animates. P12's own framing ("difference between tech demo and product") inverts against the builder.

**Fix (Day 3, hard requirement):**
- Add an explicit **audio check** button on the landing view as the **first Minute-0 beat**. Narrate it as "radio station power-on" for ~2 sec. User clicks. A 1-sec audible chime plays. AudioContext is unlocked.
- **ONE shared AudioContext** for the whole page. Pass it into every `<audio>` element + the music filler. No per-element context creation.
- Day 6 rehearsal: log to console which `<audio>` elements successfully began playing. Surface a red banner if any failed.

### 2. Day 3 overloaded (severity: CRITICAL) — A-Scope + A-Feasibility

Master's Day 3 entry stacks: SSE + pitch-round UI + Huxe card + ⟲-15s + ⏭ + long-press + segment-chip track + music filler + P13 pipelined queue + gapless crossfades. Claimed ~80 LoC of TypeScript; realistic is 150-200 LoC of fiddly browser audio. HTML5 `<audio>` + Web Audio crossfades are notoriously tricky.

**Fix (build plan split):**
- **Day 3** ships: SSE subscriber + pitch-round animation + basic `<audio>` + card shell + ⟲-15s + ⏭ + music filler + AudioContext unlock. That's it.
- **Day 4 morning** adds: P13 client-queue + gapless transitions + long-press detector.
- **Day 4 afternoon** adds: Memory Update panel (Approach B).
- **Deferred to v1:** segment-chip click-to-jump. Ship chip track as a passive visual; clicking does nothing in v0.
- **Hard cut if running late:** long-press detector. `⏭` + `⟲` alone are enough for the learning-loop demo beat.

### 3. `segment_position_sec` on replay captures wrong moment (severity: medium) — A-Clarity

Master says on replay-15 → `audio.currentTime -= 15` + POST `segment_position_sec: audio.currentTime`. That's the AFTER-rewind position. The useful signal is "where did they rewind FROM" — the BEFORE-rewind position tells the domain agent what the user wanted to re-hear.

**Fix:**
```typescript
// WRONG (as written in master):
audio.currentTime -= 15;
post('/react', { type: 'replay', segment_position_sec: audio.currentTime });

// RIGHT:
const capturedPosition = audio.currentTime;
audio.currentTime -= 15;
post('/react', { type: 'replay', segment_position_sec: capturedPosition });
```

Apply the same capture-before pattern on skip: `segment_position_sec` is the playhead at the moment of skip, BEFORE `audio.currentTime = nextSegmentStart`.

### 4. End-session button missing from card (severity: medium) — A-Completeness

The Memory Update beat (Minute 5-6) fires on `session.ended`. Master has no End-session button drawn on the card, only "tap End session (or auto-trigger after N seconds of listener inactivity)" — N unspecified.

**Fix (Day 4 afternoon):**
- Add a visible **End session** button to the card (bottom-right, subtle until episode reaches end-of-queue).
- Auto-trigger at **N=15 sec** of inactivity (no playback, no `/react` events, no navigation). Timer resets on any event.
- The button fires `session.ended` immediately; auto-trigger fires after N sec. Both paths flow to the same memory update SSE sequence.

### 5. Segment-chip click-to-jump (severity: low, deferred)

Ship chip track as passive visual in v0. Clicking does nothing. Navigation-without-telemetry is ambiguous signal (did the user skip, or was it just exploration?) — defer to v1 when we have more signal-to-noise evidence.

## Open questions (component-scoped)

- **Long-press gesture on desktop screen-share:** does 400ms hold reliably trigger when clicking via trackpad / mouse during demo? Day 6 rehearsal: test explicitly. If unreliable, add a small "..." menu button on the current segment card as fallback.
- **Card keyboard shortcuts:** space = play/pause, ← = ⟲-15, → = ⏭ skip. Cheap to add. Day 4 afternoon.
- **Mobile responsive:** demo is screen-shared from laptop; mobile is v1. Don't spend Day 3-4 time on mobile-specific CSS.

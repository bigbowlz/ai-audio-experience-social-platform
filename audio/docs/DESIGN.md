# Component: `audio`

**Status:** DRAFT (component extract from master design)
**Master doc:** [`~/.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md`](../../../../.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md) — canonical source.
**Reviewed:** 2026-04-13 (spec review 6/10, red-team)

## Purpose

Everything that turns a Script into listenable audio on the user's device:
1. **TTS generation** — narrator voice + guest voice (P5), ElevenLabs
2. **Streaming segment pipeline** (P13) — segment 1 critical-path; 2–N background
3. **Music beds** (P12) — idle filler during orchestration + backpressure absorber between segments
4. **Offline concat** — ffmpeg post-rehearsal assembles single-file Episode A MP3 for post-demo Slack handoff (NOT on live-gen path)

## Key premises

- **P5** Narrator + guest voice; no dialogue. Main voice for internal agents, distinct voice (ideally Alice-cloned) for `@AlicesLens`
- **P12** Idle music filler during orchestration waits; duck on segment-1 arrival; re-fade for backpressure gaps
- **P13** Streaming segment TTS; time-to-first-audio ~3-5 sec, not ~15 sec

## Interface contract

```python
class TTSClient:
    async def synthesize(
        text: str,
        voice_id: str,
        episode_id: str,
        segment_index: int,
        *,
        priority: Literal["critical", "background"],
    ) -> SegmentPath:
        """
        Writes MP3 to ./data/episodes/{episode_id}/segment_{segment_index}.mp3.
        Returns the relative URL /audio/{episode_id}/segment_{segment_index}.mp3
        (served by api-storage's /audio route from local FS with range support).

        critical: awaited, blocks return until disk write completes.
        background: fire-and-forget; completion emits audio.segment.done SSE
                    with the URL to the player via api-storage.
        """

class MusicFiller:
    def start(context: AudioContext) -> None:
        """On episode.started: play randomly-selected transition bed at -18 dB."""
    def duck(over_ms: int = 1500) -> None:
        """On first audio.segment.done: crossfade out."""
    def fade_in_backpressure(over_ms: int = 500, target_db: int = -12) -> None:
        """On audio.segment.delayed: fade back up to cover gap."""
```

## Voices (from master, clarified)

- **Main narrator:** pick on Day 2. Conversational-but-confident, not news-anchor-stiff. Test: read a 2-paragraph sample in your actual headphones and ask "would I listen to this every morning?"
- **`@AlicesLens` guest voice:** distinctly different from narrator. Ideal: clone from Alice's 2-min sample captured Day 0. Fallback: stock ElevenLabs voice different gender/accent from narrator. Commit by Day 4.
- **SSML / pronunciations:** handle `@ofmiles` → `"ofmiles"`, `CPI` → `"C P I"` on first mention. Post-LLM regex pass, not SSML. Day 5 morning rehearsal listen-through adds to the regex.

## Music beds

- 5-8 royalty-free beds (YouTube Audio Library / Pixabay), pre-downloaded Day 0 or Day 1 end-of-day (see Reviewer Concern #4)
- **Earmark 1-2 as transition beds for P12**: ambient texture, no melody, no beat drop. Should feel like the air-conditioning hum of a radio studio, not a song.
- Other beds used between segments for tone (upbeat / mid / chill / outro)

## Dependencies on other components

| Component | Contract | Direction |
|---|---|---|
| `producer` | consumes per-segment script output via async iterator | in |
| `player` | consumes local-FS URLs (`/audio/:episode_id/:segment_n`) via SSE `audio.segment.done` events | out |
| `api-storage` | owns the `/audio/:episode_id/:segment_n` route (reads from disk, streams with range support); emits `audio.*` SSE | out |

## Build plan touchpoints

- **Day 0 or Day 1 end-of-day:** download 5-8 music beds. Earmark transition beds.
- **Day 2:** ElevenLabs integration. Pick narrator voice (record + listen). Straight parallel TTS (not pipelined) end-to-end. Per-segment MP3 written to `./data/episodes/{episode_id}/segment_{n}.mp3`. Run offline ffmpeg concat on one cached episode → single-file MP3 at `./exports/episode-{episode_id}.mp3` for post-demo handoff. End-of-day: CLI → real listenable 20-min episode end-to-end (browser plays segments via the `/audio/:episode_id/:segment_n` route owned by api-storage).
- **Day 3 (morning):** P13 client pipelining. TTS wrapper with `priority` flag. `audio.segment.done` emitted per segment as URL becomes available. Music filler wired. See Reviewer Concern #6 for why P13 moves to Day 3 morning, not Day 2.
- **Day 4:** `@AlicesLens` guest voice. Voice cloning if Alice recorded on Day 0. Otherwise stock fallback. Test opener + Alice segment in full rehearsal.
- **Day 5 morning:** rehearsal listen-through, tune pronunciation regex.

## Success criteria

- Day 2: narrator voice passes the "headphones test"
- Day 3: segment 1 listenable in ≤5 sec from `write_script()` emit
- Day 3: music filler starts on `episode.started`, crossfades out cleanly on segment-1 arrival
- Day 4: guest voice is instantly distinguishable from narrator
- Day 6: 2 dress rehearsals complete with zero dead-air events

## Reviewer concerns

### 1. Latency budget stale vs P13 (severity: HIGH) — A-Consistency

Master's Phase 7 ("TTS: N calls in parallel ~5-12 sec wall-clock") describes the pre-P13 flow. The perceived-latency table ("segment 1 ~3-5 sec") describes P13. Both coexist in the master doc.

**Fix:** the correct description is:
- Segment 1 (critical): ~3-5 sec TTS, disk write ~50 ms
- Segments 2-N (background): total wall-clock overlaps with segment 1 playback; never blocks first audio
- Time-to-first-listenable: ~5 sec

Master's latency section needs a back-propagation edit. Tracked in the master-level fixes summary; not this component's code.

### 2. ffmpeg is NOT on live-gen path (severity: HIGH) — A-Consistency

Master's architecture diagram and Open Question 4 agree ffmpeg is offline-only. Master's latency budget still lists "ffmpeg assembly ~2-4 sec" as a live phase.

**Fix:** live-gen streams per-segment MP3s directly to the player; ffmpeg concat runs post-rehearsal to produce the single-file Episode A MP3 for Slack/Drive handoff. Document this split clearly in the TTS wrapper's README.

### 3. Opener length pinned to ~30 sec for P13 math (severity: medium) — A-Consistency

Master storyboard Minute 1-3 says "LIVE TTS of the OPENING segment only (~30-60 sec audio, ~8-12 sec to generate)" which contradicts P13's ~5 sec claim. A 60-sec TTS really does take ~10 sec on ElevenLabs.

**Fix:** pin opener to ~30 sec in the script generator's per-segment constraints. Producer knows "first segment target_length ≈ 30 sec." Keeps P13's first-audio claim honest.

### 4. Music beds Day 0 prep (severity: medium) — A-Completeness

P12 unlocks AudioContext on Day 3, but beds were scheduled for Day 2 morning. If Day 2 slips (likely), Day 3 has nothing to play.

**Fix:** download beds on Day 0 or Day 1 end-of-day (~15 min). Earmark 1-2 as transition beds explicitly. Add to Day 0 checklist.

**Pre-demo checklist item (Day 6 + Day 7):** Zoom "Share Sound" toggle MUST be enabled when screen-sharing. Without it the exec panel hears silence even if local audio works perfectly.

### 5. `audio.segment.delayed` SSE event missing (severity: medium) — A-Completeness

P13 backpressure case (segment k+1 not ready when k ends) needs a signal to the music filler and the player. Master has no such event.

**Fix:** emit `audio.segment.delayed {segment_index, eta_ms}` when the player reports an underrun. Music filler listens; fades back in at -12 dB. Player UI can show a subtle "finalizing next segment…" toast if gap exceeds 3 sec. Coordinate with `api-storage` component.

### 6. P13 pipelining moved from Day 2 to Day 3 morning (severity: medium) — A-Feasibility

Day 2 already stacks ElevenLabs integration + narrator voice pick + 5-8 music beds download + per-segment TTS + local disk writes + offline ffmpeg concat. Adding P13 pipelining same day is ambitious for a first-time-TTS integrator.

**Fix:** Day 2 ships straight parallel TTS (not pipelined) end-to-end — a listenable episode by end of day. Day 3 morning adds the P13 pipeline semantics (priority flag, background fire-and-forget, `audio.segment.done` streaming). Buys breathing room without dropping scope.

### 7. TTS budget under-scoped (severity: low) — A-Feasibility

Master budgets $75 (15 rehearsal gens × $5). Realistic is 25-30 gens (two-episode flow doubles per-rehearsal cost, plus dev iterations): $125-150.

**Fix:** budget $150. Update master Dependencies total.

## Open questions (component-scoped)

- **Voice consistency across segments:** if the same narrator voice reads 5 segments with different topic moods, is tone uniform? ElevenLabs has a "stability" parameter. Day 2 rehearsal: sample at stability 0.4 vs 0.6 vs 0.8 and pick.
- **Music bed licensing:** Pixabay + YouTube Audio Library are royalty-free for commercial use. Confirm the specific tracks you download are. Keep a `LICENSES.md` with source URLs.
- **Offline ffmpeg concat command:** document the exact command (`ffmpeg -f concat -safe 0 -i list.txt -c copy out.mp3`) so Day 6 rehearsal export is reproducible.

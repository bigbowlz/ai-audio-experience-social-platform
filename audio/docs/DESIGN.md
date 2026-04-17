# Component: `audio`

**Status:** DRAFT (finalized design from office-hours session, eng-reviewed 2026-04-16)
**Master doc:** [`~/.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260415-233819.md`](../../../../.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260415-233819.md) — canonical source (supersedes `wanlizhou-main-design-20260413-182237.md`).
**Finalized upstream:** [`agents/youtube/docs/DESIGN.md`](../../agents/youtube/docs/DESIGN.md), [`agents/docs/DESIGN.md`](../../agents/docs/DESIGN.md), [`agents/docs/prompt_design.md`](../../agents/docs/prompt_design.md) — binding contracts.
**Reviewed:** 2026-04-16 (eng review, 13 issues resolved, 0 unresolved)

## Purpose

Everything that turns a Script into listenable audio on the user's device:

1. **TTS generation** — narrator voice + guest voice, ElevenLabs batch API
2. **Parallel batch pipeline** — segment 1 fires first; segments 2–N fire in parallel immediately after
3. **Music beds** — idle filler during orchestration + backpressure absorber between segments
4. **Offline concat** — ffmpeg post-rehearsal assembles single-file Episode A MP3 for post-demo Slack handoff (NOT on live-gen path)

## Premises

1. **ElevenLabs batch TTS can generate a ~30-sec segment in ~5-8 seconds.** Probe validates. Music beds cover the generation wait.
2. **Streaming dropped for v0.** Batch-only with music filler is sufficient. Streaming saves ~2s that the music bed already masks — not worth the complexity on a 6-day timeline.
3. **Per-segment MP3 files served from local disk are sufficient for gapless playback.** No CDN, no transcoding. Player loads `/audio/{episode_id}/segment_n.mp3` directly.
4. **Music beds are pre-downloaded static assets, not AI-generated.** Royalty-free libraries (YouTube Audio Library, Pixabay). Zero runtime cost, zero latency.
5. **No voice cloning for v0.** Alice gets a stock ElevenLabs voice. Narrator gets a stock voice. Both from ElevenLabs library.
6. **ffmpeg offline concat is NOT on the live-gen path.** Live path serves per-segment MP3s. ffmpeg assembles single-file handoff MP3 post-rehearsal only.
7. **Producer `write_script()` returns `list[SegmentScript]`, not an async iterator.** All segments are available at once, enabling parallel batch dispatch.
8. **Budget:** $20 ElevenLabs pay-as-you-go (~320-400K chars, ~60-80 full episodes). Warn at 80% of verified budget.
9. **Localhost-only (P4).** No CDN, no hosted audio. Per-segment MP3s served from local disk.

## Phase 0: ElevenLabs API Probe

Before locking implementation, run an empirical probe against the ElevenLabs API — same pattern as the YouTube Data API probe (`tmp/ydata/probe_1776208130/`).

### Probe objectives

| #   | Question                                        | Method                                                                                                                                                                      | Decision it informs                                                                 |
| --- | ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| 1   | **Streaming latency: time to first audio byte** | Call `/v1/text-to-speech/{voice_id}/stream` with a ~30-sec segment (~500 chars). Measure wall-clock from request send to first chunk received. Run 5 times, report p50/p95. | **Informational only.** Streaming dropped for v0. Data logged for v1 consideration. |
| 2   | **Batch latency: full segment generation**      | Call `/v1/text-to-speech/{voice_id}` (non-streaming). Same text. Measure total wall-clock to complete MP3. Run 5 times.                                                     | **Primary gate.** Batch p50 must be < 10s. This is the v0 architecture.             |
| 3   | **Model comparison: Turbo v2.5 vs Flash**       | Run probes 1+2 on both `eleven_turbo_v2_5` and `eleven_flash_v2_5`. Compare latency AND subjective quality.                                                                 | Model selection for the design.                                                     |
| 4   | **Voice audition**                              | Generate the same ~30-sec sample on 3-4 narrator candidates (conversational, not news-anchor). Listen on headphones.                                                        | Narrator voice pick.                                                                |
| 5   | **Output format validation**                    | Request `output_format=mp3_44100_128` (standard) and `mp3_22050_32` (low quality). Compare file sizes and subjective quality.                                               | Whether lower quality is acceptable without audible degradation.                    |
| 6   | **Concurrent requests**                         | Fire 4 batch requests in parallel (simulating segments 2-5). Measure if ElevenLabs throttles or degrades.                                                                   | Whether parallel batch for background segments is viable.                           |
| 7   | **Pay-as-you-go feature check**                 | Verify: how many voices in library? Any rate limits? SDK version compatibility?                                                                                             | Confirm no tier-gated features block the design.                                    |

### Probe script location

`audio/scripts/elevenlabs_probe.py` — Python (reuses the venv from the YouTube probe). Saves results to `tmp/audio_probe/` with timestamped JSON + generated MP3 samples.

### Sample text for probe

Use a real-ish script segment (~500 chars, ~30 sec spoken):

```
You've been on a jazz deep dive lately. Three channels you follow have been dropping
new content, and your recent likes are full of modal jazz and neo-soul crossovers.
Anjunadeep, a channel you've followed since 2019, just released a live session that
blends electronic textures with acoustic jazz instrumentation. Meanwhile, Adam Neely
posted a theory breakdown of Kamasi Washington's latest album that's been sitting in
your liked videos. Let's talk about what's pulling you in.
```

### Probe success gates

- **Batch full-file p50 < 10 seconds for ~30-sec segment** — batch viable (music bed covers the wait)
- **Concurrent batch: no throttling on 4 parallel requests** — parallel dispatch for segments 2-N is viable
- **At least 1 narrator voice passes the headphones test** — proceed with voice selection
- **Streaming latency (informational):** measure for v1 consideration. If streaming full-file < batch full-file by >=2 sec, log as a v1 optimization opportunity

## Architecture: Batch TTS Pipeline

All segments use the ElevenLabs batch API. Segment 1 fires first, segments 2-N fire in parallel immediately after. Music beds play throughout the generation wait.

**Why batch-only (streaming dropped):**

- One code path. No streaming chunk reassembly, no partial-file discard, no streaming-to-batch fallback logic.
- Music beds cover the ~5-8s generation wait for segment 1. The user hears ambient music immediately on clicking Generate, then speech starts. The perceived experience is the same as streaming.
- Parallel dispatch for segments 2-N means background generation overlaps with segment 1 playback. Total pipeline wall-clock is dominated by segment 1 generation time.
- If v1 needs faster first-audio, streaming can be added as an optimization on the batch path with no architectural change (the SDK supports both).

```
Producer.write_script() returns list[SegmentScript]
         │
         ├─── Segment 1 ──► Batch TTS ──► disk write
         │                                  ├─► SSE: audio.segment.done
         │                                  └─► Player starts playback
         │
         └─── Segments 2-N (parallel) ──► Batch TTS ──► disk write
                                              └─► SSE: audio.segment.done (per segment)
```

## SSE Event Contracts (audio-owned)

Audio emits these events via the integration spine (api-storage's SSE stream). Payloads are defined here; wire format is owned by api-storage.

| Event                   | Payload                                              | Emitted by                             | When                                                                                                                                           |
| ----------------------- | ---------------------------------------------------- | -------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `audio.segment.done`    | `{segment_index: int, duration_ms: int, url: str}`   | TTSClient (after disk write completes) | Each segment MP3 is fully written to disk                                                                                                      |
| `audio.segment.delayed` | `{segment_index: int, eta_ms: int}`                  | Player (client-side)                   | Player's segment queue underruns (segment k finishes playing, k+1 not yet in queue). `eta_ms` is telemetry only; `-1` if unknown.              |
| `episode.done`          | `{total_segments: int, skipped_segments: list[int]}` | Orchestrator                           | All segments have been processed (successfully or skipped). Player knows the episode is complete and which segments were skipped (422 errors). |
| `episode.failed`        | `{reason: str}`                                      | Orchestrator                           | All segments failed after retries, or total pipeline timeout (120s) exceeded. MusicFiller.stop() fires. Player shows error state.              |

**Segment ordering contract:** `audio.segment.done` events may arrive out of order (parallel batch dispatch completes non-deterministically). The consumer (Player) MUST buffer segments and play in `segment_index` order. The Player uses the `<audio>` element's `ended` event for transition timing, not the pre-reported `duration_ms`.

**`audio.segment.delayed` ownership:** the **player** detects underruns client-side (it knows when playback of segment k ends and whether segment k+1's URL is in its queue).

**v0 simplification:** MusicFiller detects underruns locally in the player without a server round-trip. The player's `SegmentQueue.onUnderrun()` callback triggers `MusicFiller.fadeInBackpressure()` directly. No server endpoint needed. MusicFiller ignores `eta_ms`; it fades in unconditionally on underrun and ducks on the next `audio.segment.done`. The server-side re-broadcast of `audio.segment.delayed` (for logging/telemetry) is deferred to when api-storage is finalized.

## ElevenLabs Integration

Uses the official `elevenlabs` Python SDK instead of raw HTTP. The SDK handles auth, retries, and response parsing natively. Eliminates a class of bugs in request/response handling.

### SDK usage

```python
from elevenlabs.client import ElevenLabs

client = ElevenLabs(api_key=api_key)

# Batch TTS (all segments)
audio = client.text_to_speech.convert(
    voice_id=voice_id,
    text=text,
    model_id="eleven_turbo_v2_5",     # or eleven_flash_v2_5 — decided by probe
    voice_settings={
        "stability": 0.5,             # 0.0-1.0; probe Day 2 at 0.4/0.6/0.8
        "similarity_boost": 0.75,     # default; higher = more consistent
        "style": 0.0,                 # disabled for speed
        "use_speaker_boost": True,    # clarity enhancement
    },
    output_format="mp3_44100_128",    # or lower quality if probe validates
)
# `audio` is a generator of bytes; write to disk

# Voice listing (probe only)
voices = client.voices.get_all()
```

### Endpoints (via SDK)

| SDK method                        | Used for                                   | Auth        |
| --------------------------------- | ------------------------------------------ | ----------- |
| `client.text_to_speech.convert()` | All segments (batch). Returns MP3 bytes.   | SDK-managed |
| `client.voices.get_all()`         | Probe: list available voices for audition. | SDK-managed |

### SSML / Pronunciation handling

Handle `@ofmiles` → `"ofmiles"`, `CPI` → `"C P I"` on first mention. Post-LLM regex pass, not SSML.

```python
PRONUNCIATION_RULES: list[tuple[str, str]] = [
    (r"@(\w+)", r"\1"),                  # strip @ from handles
    (r"\bCPI\b", "C P I"),               # expand common acronyms
    (r"\bGDP\b", "G D P"),
    (r"\bAI\b", "A I"),
    # Add to this list during Day 5 morning rehearsal listen-through
]

def apply_pronunciation(text: str) -> str:
    for pattern, replacement in PRONUNCIATION_RULES:
        text = re.sub(pattern, replacement, text)
    return text
```

Rules are applied BEFORE sending text to ElevenLabs. The list grows during rehearsal listen-throughs.

## Interface contract

### `TTSClient`

```python
class TTSClient:
    """
    Synthesizes text to per-segment MP3 files on local disk.
    Uses ElevenLabs Python SDK. Batch-only.
    """

    def __init__(
        self,
        api_key: str,
        output_dir: str = "./data/episodes",
        model_id: str = "eleven_turbo_v2_5",    # decided by probe
        max_concurrent: int = 4,        # semaphore limit for parallel batch;
                                        #   adjust based on probe objective 6
    ): ...

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        episode_id: str,
        segment_index: int,
    ) -> SegmentResult:
        """
        Writes MP3 to ./data/episodes/{episode_id}/segment_{segment_index}.mp3.
        Applies pronunciation rules before sending text to ElevenLabs
        (all text, regardless of voice/speaker).

        Uses batch API via ElevenLabs SDK. Caller dispatches concurrently;
        internally awaits completion before returning. Concurrency bounded
        by max_concurrent semaphore.

        Returns SegmentResult with the relative URL and timing metadata.
        """

class SegmentResult(TypedDict):
    segment_index: int
    url: str                        # "/audio/{episode_id}/segment_{segment_index}.mp3"
    duration_ms: int                # audio duration, parsed from MP3 header via mutagen
    duration_estimated: bool        # True if mutagen failed and duration was estimated
                                    #   from character count (~150 chars/sec spoken)
    generation_time_ms: int         # wall-clock TTS generation time
    character_count: int            # chars in the successful request
    billed_character_count: int     # total chars billed including failed retries
                                    #   (ElevenLabs bills per-attempt)
```

### Batch path (all segments)

```python
async def _synthesize_batch(self, text: str, voice_id: str,
                             output_path: Path) -> SegmentResult:
    """
    1. Call client.text_to_speech.convert() via SDK
    2. Await full response (complete MP3 bytes)
    3. Write to disk atomically
    4. Parse duration via mutagen (fallback: estimate from char count)
    5. Return SegmentResult
    """
```

### Voice mapping

```python
# audio/config.py
VOICE_MAP: dict[str, str] = {
    "youtube":   NARRATOR_VOICE_ID,
    "calendar":  NARRATOR_VOICE_ID,
    "weather":   NARRATOR_VOICE_ID,
    "alices":  GUEST_VOICE_ID,
}
```

The orchestrator looks up `VOICE_MAP[segment.agent]` and passes the voice_id to `synthesize()`. All agents default to narrator voice. Alice's agent uses the guest voice. Deterministic, no LLM decision.

### Parallel dispatch

Producer returns `list[SegmentScript]`. The orchestrator fires segment 1 first, then segments 2-N in parallel:

```python
# Caller pattern (in the orchestrator, not in TTSClient):
segments: list[SegmentScript] = producer.write_script(...)

# Segment 1 first (critical path — user is waiting)
seg1 = segments[0]
seg1_result = await tts.synthesize(
    seg1.script, VOICE_MAP[seg1.agent], episode_id, 0
)
emit_sse("audio.segment.done", seg1_result)

# Fire remaining segments in parallel
background_tasks = [
    tts.synthesize(seg.script, VOICE_MAP[seg.agent], episode_id, seg.index)
    for seg in segments[1:]
]
for coro in asyncio.as_completed(background_tasks):
    result = await coro
    emit_sse("audio.segment.done", result)

# After all complete (or fail):
emit_sse("episode.done", {
    "total_segments": len(segments),
    "skipped_segments": [i for i in range(len(segments)) if i not in completed],
})
```

### `MusicFiller` (browser-side, TypeScript)

```typescript
class MusicFiller {
  private audioContext: AudioContext; // shared with all <audio> elements
  private gainNode: GainNode;
  private source: AudioBufferSourceNode | null = null;
  private beds: AudioBuffer[]; // pre-loaded on page load

  constructor(audioContext: AudioContext, beds: AudioBuffer[]) {
    this.audioContext = audioContext;
    this.gainNode = audioContext.createGain();
    this.gainNode.connect(audioContext.destination);
    this.beds = beds;
  }

  start(): void {
    // Pick a random transition bed
    const bed = this.beds[Math.floor(Math.random() * this.beds.length)];
    this.source = this.audioContext.createBufferSource();
    this.source.buffer = bed;
    this.source.loop = true;
    this.source.connect(this.gainNode);
    this.gainNode.gain.setValueAtTime(
      this.dbToGain(-18), // -18 dB: audible but quiet
      this.audioContext.currentTime,
    );
    this.source.start();
  }

  duck(overMs: number = 1500): void {
    // Crossfade out when first segment audio arrives
    this.gainNode.gain.linearRampToValueAtTime(
      0,
      this.audioContext.currentTime + overMs / 1000,
    );
  }

  fadeInBackpressure(overMs: number = 500, targetDb: number = -12): void {
    // Fade back in during segment gaps
    this.gainNode.gain.linearRampToValueAtTime(
      this.dbToGain(targetDb),
      this.audioContext.currentTime + overMs / 1000,
    );
  }

  stop(): void {
    this.gainNode.gain.linearRampToValueAtTime(
      0,
      this.audioContext.currentTime + 1.0,
    );
    setTimeout(() => {
      this.source?.stop();
      this.source = null;
    }, 1100);
  }

  private dbToGain(db: number): number {
    return Math.pow(10, db / 20);
  }
}
```

**AudioContext user-gesture requirement:** Modern browsers require a user gesture to start `AudioContext` (autoplay policy). The `AudioContext` MUST be created (or resumed via `audioContext.resume()`) inside the Generate button's click handler. `MusicFiller` receives a pre-activated `AudioContext`. Without this, the music bed is silently blocked on first page load — a demo-day disaster with Zoom screen-share.

## Error Handling Matrix

Per-class handling, same pattern as the youtube spec's `fetch_context()` failure handling.

**Per-request timeout:** 60 seconds. **Total pipeline timeout:** 120 seconds — if zero segments succeed within 120s, emit `episode.failed` and stop. Music beds cover partial waits.

| HTTP Status / Error                                 | Class                          | Action                                                                                                   |
| --------------------------------------------------- | ------------------------------ | -------------------------------------------------------------------------------------------------------- |
| `401 Unauthorized` (bad API key)                    | Unrecoverable                  | Fail loud. Log error. Emit `episode.failed`. Cannot proceed without valid key.                           |
| `422 Unprocessable` (text too long, invalid params) | Unrecoverable for this segment | Log + skip segment (add to `skipped_segments`). Likely a bug in text prep.                               |
| `429 Too Many Requests` (rate limit)                | Recoverable shortly            | Exponential backoff (1s, 2s, 4s), retry up to 3 times. Pay-as-you-go rate limits are generous but exist. |
| `500/502/503` (ElevenLabs backend)                  | Transient                      | Exponential backoff, retry up to 3 times.                                                                |
| Network timeout (no response within 60s)            | Transient                      | Retry once. If still timeout, log + skip segment.                                                        |

**Cost tracking:** Every synthesis attempt (including failed retries) adds to `billed_character_count`. ElevenLabs bills per-attempt, not per-success. The running total uses `billed_character_count`, not `character_count`. If cumulative billed cost approaches the $20 budget, log a warning. Do not hard-stop generation (the user can fund more), but surface it.

## Voices

### Narrator voice

- **Selection:** during probe, audition 3-4 candidates from ElevenLabs library. Criteria: conversational-but-confident, not news-anchor-stiff.
- **Test:** read the probe sample text. Listen on actual headphones. Ask: "would I listen to this every morning?"
- **Stability parameter:** tune during Day 2 rehearsal. Sample at `stability` 0.4, 0.6, 0.8. Higher = more consistent across segments, lower = more expressive but variable.
- **Commit by:** Day 2 end of day.

### Alice's voice (`@AlicesLens` guest voice)

- **Selection:** a stock ElevenLabs voice. No cloning for v0.
- **Strategy:** pick a voice with different gender, accent, or vocal register from narrator. Must be instantly distinguishable.
- **Commit by:** Day 4 (when `alices_agent` is built).

### Voice configuration

```python
# audio/config.py
NARRATOR_VOICE_ID = "..."       # set after Day 2 audition
GUEST_VOICE_ID = "..."          # set on Day 4
ELEVENLABS_MODEL = "..."        # set after probe (turbo_v2_5 or flash_v2_5)
```

## Music beds

- **Quantity:** 3-5 royalty-free transition beds. Fewer than the original draft's 5-8 — we only need ambient textures, not a per-segment-mood library.
- **Source:** YouTube Audio Library (free, cleared for commercial use) and/or Pixabay Audio.
- **Criteria:** ambient texture, no melody, no beat drop. Should feel like the air-conditioning hum of a radio studio, not a song. 15-30 seconds each, loopable.
- **Format:** MP3, pre-downloaded to `audio/assets/beds/`. Committed to repo (small files, royalty-free).
- **When to download:** Day 0 or Day 1 end-of-day. Takes ~15 minutes.
- **Licensing:** Include `audio/assets/beds/LICENSES.md` with one line per bed: `filename.mp3 | source URL | license type (e.g., CC0, YouTube Audio Library TOS)`.

## Gapless Transition Strategy

MP3 encoder padding can cause ~20-50ms silence between segments. For v0, accept this and mitigate in the player with a **50ms crossfade** between the end of segment k and the start of segment k+1. The music filler's backpressure fade-in at -12 dB also masks any gap that exceeds the crossfade window. This is cheap to implement (Web Audio `linearRampToValueAtTime`) and avoids the complexity of `lame --nogap` re-encoding or container-level fixes. Revisit for v1 if the gap is audibly distracting during rehearsals.

## Offline Concat (post-rehearsal only)

NOT on the live-gen path. Runs after a rehearsal to produce a single-file Episode A MP3 for Slack/Drive handoff to judges.

```bash
# Exact command (documented for reproducibility). macOS-safe numeric sort.
# Step 1: generate concat list with correct numeric ordering
python3 -c "
import glob, re
files = sorted(
    glob.glob('./data/episodes/{episode_id}/segment_*.mp3'),
    key=lambda f: int(re.search(r'segment_(\d+)', f).group(1))
)
print('\n'.join(f\"file '{f}'\" for f in files))
" > /tmp/ffmpeg_concat_list.txt

# Step 2: concat
ffmpeg -f concat -safe 0 -i /tmp/ffmpeg_concat_list.txt -c copy \
  ./exports/episode-{episode_id}.mp3
rm /tmp/ffmpeg_concat_list.txt
```

Note: `ls -v` (GNU numeric sort) is NOT available on macOS. The Python glob+sort is portable.

Creates `./exports/` directory on first use. Both `./data/episodes/` and `./exports/` are gitignored.

## Segment Pipeline Flow (end-to-end)

```
1. User clicks Generate
   └─► AudioContext created/resumed in click handler (autoplay policy)
   └─► episode.started SSE
   └─► MusicFiller.start() — ambient bed at -18 dB

2. [Agents pitch, Producer selects, payment happens — ~15-25 sec]
   └─► Music bed plays throughout (user hears something, not silence)

3. Producer.write_script() returns list[SegmentScript]
   └─► TTSClient.synthesize(seg1) via batch API
       └─► ~5-8 sec generation time (music bed covers the wait)
       └─► Write to disk, emit audio.segment.done {index: 0, url: "..."}
   └─► MusicFiller.duck() — crossfade out over 1500ms
   └─► Player starts playing segment_0.mp3

4. While segment 1 plays (~60-90 sec of audio):
   └─► TTSClient fires segments 2-N via batch API in parallel
   └─► Each completes: write to disk, emit audio.segment.done
   └─► Player buffers and reorders by segment_index
   └─► Player enqueues for gapless transition (50ms crossfade)

5. If segment k finishes playing before segment k+1 is ready:
   └─► Player detects underrun
   └─► emit audio.segment.delayed {index: k+1, eta_ms: ...}
   └─► MusicFiller.fadeInBackpressure() at -12 dB
   └─► When segment k+1 ready: MusicFiller.duck(), play continues

6. All segments processed
   └─► episode.done SSE {total_segments, skipped_segments}
   └─► MusicFiller.stop()

7. (Failure path) Zero segments succeed within 120s
   └─► episode.failed SSE {reason: "..."}
   └─► MusicFiller.stop()
   └─► Player shows error state
```

## Dependencies on other components

### Finalized (binding contracts)

| Component                              | Relevance to audio                                                                                                                                                                                                                                                        | Status                   |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------ |
| `agents/youtube`                       | No direct dependency. Audio consumes script text, not agent output.                                                                                                                                                                                                       | Finalized (not relevant) |
| `agents/docs/DESIGN.md`                | `Pitch` shape, `DataAgent` protocol. Not consumed directly by audio.                                                                                                                                                                                                      | Finalized (not relevant) |
| `agents/docs/prompt_design.md`         | `EpisodeScript.segments[*].script` is the text audio consumes. Shape is defined here.                                                                                                                                                                                     | Finalized (binding)      |
| `producer` (via `prompt_design.md` §4) | `write_script()` returns `list[SegmentScript]`. Audio consumes the `script` field from each `SegmentScript`. All segments available at once enables parallel batch dispatch. `select_segments()` and `EpisodeScript`/`SegmentScript` shapes locked in `prompt_design.md`. | Finalized (binding)      |

### Non-finalized (interfaces TBD when those components are designed)

| Component     | Contract (expected)                                                                                                               | Direction |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------- | --------- |
| `player`      | consumes local-FS URLs via SSE `audio.segment.done` events. Must buffer and reorder by `segment_index`.                           | out       |
| `api-storage` | owns the `/audio/:episode_id/:segment_n` route (reads from disk, streams with range support); re-broadcasts `audio.*` SSE events. | out       |

## Build plan touchpoints

- **Pre-build (Day 0 / Day 1 end-of-day):**
  - Download 3-5 music beds from YouTube Audio Library / Pixabay. Earmark 1-2 as transition beds. Write `audio/assets/beds/LICENSES.md` with source URLs.
  - Run ElevenLabs probe (`audio/scripts/elevenlabs_probe.py`). Save results to `tmp/audio_probe/`. Commit probe results as regression fixtures.
  - Decisions from probe: model selection, output format, narrator voice shortlist. Streaming latency measured for informational purposes (v1).

- **Day 2: ElevenLabs integration + narrator voice pick + music filler.**
  - Install `elevenlabs` SDK + `mutagen` in venv.
  - Build `TTSClient` with batch path (batch-only, no streaming). Wire parallel dispatch.
  - Pick narrator voice from probe shortlist. Record + listen on headphones. Tune `stability` param (0.4/0.6/0.8).
  - Per-segment MP3 written to `./data/episodes/{episode_id}/segment_{n}.mp3`.
  - Wire `audio.segment.done` / `episode.done` / `episode.failed` SSE emission.
  - Music filler integration (duck on first segment, backpressure fade).
  - Run offline ffmpeg concat on one cached episode → single-file MP3 at `./exports/`.
  - End-of-day: CLI → real listenable episode end-to-end.
  - Success gate: segment 1 MP3 on disk, music filler starts on `episode.started`, ducks cleanly on segment 1 arrival.

- **Day 3 (morning): Freed for rehearsal/polish.**
  - First full listen-through with music beds + segment transitions.
  - Test parallel batch dispatch with 4+ segments. Verify no ElevenLabs throttling.
  - Test `episode.failed` path (temporarily invalid API key).
  - Fix any audio quality issues found.

- **Day 4: Alice's guest voice.**
  - Pick stock ElevenLabs voice for `@AlicesLens` (distinct from narrator).
  - Test opener + Alice segment in full rehearsal.

- **Day 5 morning: Rehearsal + pronunciation tuning.**
  - Full listen-through. Add pronunciation rules as needed.
  - Tune any voice quality issues found during rehearsals.

- **Day 6 + Day 7 pre-demo:**
  - 2 dress rehearsals with zero dead-air events.
  - Pre-demo checklist: Zoom "Share Sound" toggle MUST be enabled when screen-sharing. Without it the exec panel hears silence even if local audio works perfectly.
  - Export final Episode A to `./exports/` for judge handoff.

## Success criteria

- **Probe:** batch p50 < 10 sec for ~30-sec segment. Concurrent batch (4 parallel requests) shows no throttling. At least 1 narrator voice passes the headphones test. Streaming latency measured for informational purposes (v1 consideration).
- **Day 2:** narrator voice passes the headphones test. Full episode listenable via CLI. Music filler starts on `episode.started`, ducks cleanly on segment 1 arrival. `episode.done` emits with correct segment count.
- **Day 4:** guest voice instantly distinguishable from narrator.
- **Day 6:** 2 dress rehearsals complete with zero dead-air events. Zoom "Share Sound" verified.

## Reviewer concerns (from original draft — all resolved)

All 7 reviewer concerns from the original draft (2026-04-13, spec review 6/10) have been resolved in the master doc's office-hours session + eng review:

1. **Latency budget stale vs P13** — resolved: streaming dropped, batch-only with music filler. Latency budget is simply batch generation time (~5-8s) covered by music bed.
2. **ffmpeg is NOT on live-gen path** — resolved: explicitly documented. Live path serves per-segment MP3s; ffmpeg concat is post-rehearsal only.
3. **Opener length pinned to ~30 sec** — resolved: Producer owns segment lengths via `DEFAULT_SEGMENT_SEC` (see `prompt_design.md` §4). First segment is constrained by Producer, not audio.
4. **Music beds Day 0 prep** — resolved: download on Day 0 or Day 1 end-of-day. Reduced to 3-5 beds.
5. **`audio.segment.delayed` SSE event missing** — resolved: defined in SSE event contracts above. Player-owned, client-side detection.
6. **P13 pipelining moved from Day 2 to Day 3 morning** — resolved: streaming dropped entirely. Day 2 ships batch-only end-to-end. Day 3 morning freed for rehearsal/polish.
7. **TTS budget under-scoped** — resolved: budget is $20 pay-as-you-go (~320-400K chars). Warn at 80%.

## Open questions (component-scoped)

- **Voice consistency across segments:** if the same narrator voice reads 5 segments with different topic moods, is tone uniform? Day 2 rehearsal: sample at stability 0.4/0.6/0.8 and pick.
- **ElevenLabs rate limits on pay-as-you-go:** the probe will surface these. If limits are tight (e.g., 2 concurrent requests), reduce `max_concurrent` on TTSClient accordingly.
- **Cost tracking UX:** should the dev see a running cost counter during rehearsals? Useful for visibility even with the $20 budget. Low priority but easy to add (log `billed_character_count` from each `SegmentResult`).

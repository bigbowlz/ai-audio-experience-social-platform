# Component: `api-storage`

**Status:** DRAFT (component extract from master design)
**Master doc:** [`~/.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md`](../../../../.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md) — canonical source.
**Reviewed:** 2026-04-13 (spec review 6/10, red-team)

## Purpose

The integration spine. Every other component wires through here:

1. **Next.js API routes** — `/generate` (SSE), `/react`, `/episode/:id`, `/audio/:episode_id/:segment_n`. Localhost on `npm run dev`.
2. **SSE event stream** — `/generate` streams pitch-round + producer + payment + audio + memory events to the UI.
3. **Supabase schema** — 3 Postgres tables only (`agent_memory`, `episodes`, `signals`). No Supabase Storage.
4. **Local audio filesystem** — per-segment MP3s at `./data/episodes/{episode_id}/segment_{n}.mp3` during live-gen. Final concat MP3 at `./exports/episode-{episode_id}.mp3` for direct judge handoff. Served by the `/audio/:episode_id/:segment_n` route with range-request support.

## Key premises

- **P4** Localhost-only during tournament; no public UI. MP3 is handed directly to the internal judge panel via Slack/email/Drive — no public bucket, no signed URL, no open-internet surface.
- Implements the integration contract for P8 (live + cached fallback), P9 (agent memory persistence), P10 (payment SSE), P11-P13 (player + music + streaming TTS)

## API routes

```typescript
// POST /generate { selected_agents: string[], target_length_sec: number }
// → text/event-stream (SSE)
// Emits events in master's SSE event schema.
// Generates episode end-to-end: pitch round → producer decision → payment → external pitch
//   → select → stream_episode_script → (cold_open in parallel) → generate_episode_audio (iterator-fed TTS) → sign_off.

// POST /react { episode_id, event: SignalEvent }
// → 200 OK
// Appends to signals table.
// In hackathon mode: rejects any user_id != "demo-user".

// GET /episode/:id
// → { episode_id, running_order, selected_agents, segment_count, duration_sec,
//     segment_urls: ["/audio/{id}/segment_0.mp3", "/audio/{id}/segment_1.mp3", ...] }
// segment_urls point to the /audio route below; client feeds them to <audio> elements sequentially (P13 queue).

// GET /audio/:episode_id/:segment_n
// → audio/mpeg, streams from ./data/episodes/{episode_id}/segment_{n}.mp3
// Supports HTTP Range requests (required for player ⟲-15s and ⏭ seeking).
// 404 if file doesn't exist; 403 if episode_id doesn't belong to demo-user (hackathon mode).
// ~30 lines of TypeScript total (fs.createReadStream + range parsing + Content-Length).
```

## SSE event schema (from master, with Reviewer Concern #1 additions)

Per-event reasoning-phrase convention: every agent-facing event carries `reasoning_summary` (≤80 chars).

**Orchestration events (in sequence):**
- `episode.started` `{episode_id, selected_agents}`
- `agent.pitching.started` `{agent, phase, reasoning_summary}` — **`phase` added**
- `agent.pitch.emitted` `{agent, phase, pitch, priority}` — **`phase` added**
- `agent.pitching.done` `{agent, phase, total_pitches, reasoning_summary}` — **`phase` added**
- `producer.external_decision.started` `{reason, reasoning_summary}`
- `producer.marketplace.queried` `{candidates, reasoning_summary}`
- `producer.external_agent.selected` `{agent, display_name, rationale, reasoning_summary}`
- `payment.initiated` `{from, to, amount_usdc, chain, mode_badge, reasoning_summary}` — `mode_badge` from PaymentResult
- `payment.confirmed` `{tx_hash, basescan_url, amount_usdc, mode_badge, reasoning_summary}`
- `payment.pending` `{tx_hash, basescan_url, mode_badge}` — **new; 5s soft-timeout**
- `payment.mocked` `{frozen_tx_hash, basescan_url, reason, mode_badge}` — **new; 10s hard-timeout**
- `(agent.pitching.* with phase="external" for @AlicesLens)`
- `producer.selecting.started` `{reasoning_summary}`
- `producer.pick` `{agent, pitch_id, allocated_sec, reasoning_summary}` — one per pick
- `producer.selecting.done` `{running_order, reasoning_summary}`
- `script.segment.started` `{segment_index, agent}`
- `script.segment.written` `{segment_index, text_preview}`
- `audio.segment.started` `{segment_index}`
- `audio.segment.done` `{segment_index, duration_ms, url}` — `url` is a relative path served by the `/audio/:episode_id/:segment_n` route (e.g. `/audio/abc-123/segment_0.mp3`)
- `audio.segment.delayed` `{segment_index, eta_ms}` — P13 backpressure signal
- `audio.assembled` `{episode_id, duration_ms, segment_count}` — fires when all segment MP3s are on disk (no upload step — disk write is atomic)
- `episode.done` `{episode_id, running_order, total_ms, segment_count, external_agents_invoked}`
- `error` `{stage, message}`

**Memory-update events (session-end, gated by `APPROACH_B=true`):**
- `session.ended` `{episode_id, signals_count}`
- `memory.update.started` `{agent, reasoning_summary}`
- `memory.update.decided` `{agent, action, deltas, reasoning_summary}`
- `memory.update.applied` `{agent, final_memory_snapshot}` — optional, for debug
- `memory.update.done` `{total_agents_updated, total_no_update}`

## Supabase Postgres schema (lean; no Storage bucket)

Audio lives on the local filesystem at `./data/episodes/{episode_id}/segment_{n}.mp3`; paths are derived from `episode_id`, not stored as URLs.

```sql
create table agent_memory (
  user_id    text not null,
  agent_name text not null,
  memory     jsonb not null default '{}'::jsonb,
  updated_at timestamptz default now(),
  primary key (user_id, agent_name)
);

create table episodes (
  episode_id      text primary key,              -- UUID v4 per /generate call
  user_id         text not null,
  running_order   jsonb not null,
  selected_agents text[] not null,
  segment_count   int,                           -- number of MP3s on disk under ./data/episodes/{episode_id}/
  duration_sec    int,                           -- total, summed from segments
  created_at      timestamptz default now()
);

create table signals (
  id         bigserial primary key,
  user_id    text not null,
  episode_id text not null,
  event      jsonb not null,
  ts         timestamptz default now()
);
create index on signals(user_id, episode_id);
```

Local filesystem layout:
```
./data/episodes/{episode_id}/segment_0.mp3
./data/episodes/{episode_id}/segment_1.mp3
...
./exports/episode-{episode_id}.mp3    # offline ffmpeg concat for judge handoff
```

Both directories git-ignored. Created on demand at runtime.

## Dependencies on other components

| Component | Contract | Direction |
|---|---|---|
| every other component | this is the spine | bidirectional |
| `agents` | reads/writes `agent_memory` row per agent | in/out |
| `producer` | reads Producer-memory row (agent_name='producer'); drives SSE sequence | in/out |
| `payment` | emits `payment.*` SSE; reads `PAYMENT_MODE` env | in |
| `audio` | writes per-segment MP3 to `./data/episodes/{id}/`; emits `audio.*` SSE | in |
| `player` | POSTs `/react`; subscribes to `/generate` SSE; loads segment MP3s via `/audio/:episode_id/:segment_n` | out |
| `learning-loop` | policy consumer of signals + memory shapes; no code here | policy only |

## Build plan touchpoints

- **Pre-build:** Supabase project created (free tier, Postgres only — no Storage bucket). Schema SQL run. `demo-user` seeded. Create `./data/episodes/` and `./exports/` at the repo root; add both to `.gitignore`.
- **Scaffold:** Next.js scaffold with `/generate` stub that emits the full SSE sequence using mocked agent output. `/react` stub writes to signals table. `agent_memory` round-trip works.
- **`/audio/:episode_id/:segment_n` route:** read from `./data/episodes/{id}/segment_{n}.mp3`; stream with HTTP Range support; 404 if missing; 403 if episode doesn't belong to demo-user. ~30 LoC. Land this early — every live-gen slice depends on it.
- **Full SSE wiring** for pitch round + producer decisions + per-segment disk writes.
- **Payment SSE wired.** `payment.mocked` / `payment.pending` / LIVE/REPLAY badge propagation.
- **Memory update SSE sequence wired** (behind `APPROACH_B=true` flag).
- **Pre-demo:** run the offline ffmpeg concat on the final-rehearsal Episode A. Single-file MP3 lands at `./exports/episode-{episode_id}.mp3`, ready to drag-and-drop into Slack/email/Drive per judge.

## Success criteria

- Scaffold: full SSE sequence renders end-to-end with mocked agent output; Supabase dashboard shows rows in `episodes` and `signals`
- Audio route: `/audio/{id}/segment_0.mp3` streams from disk in a browser `<audio>` element with working scrub bar (range requests return 206)
- Full integration: real agent output streams via SSE; per-segment MP3s land at `./data/episodes/{id}/` as they complete
- Payment: `payment.*` events propagate with correct `mode_badge`; UI shows LIVE or REPLAY correctly
- Pre-demo: 2 dress rehearsals complete; single-file Episode A MP3 exported to `./exports/` locally, ready to send
- Post-demo: ≥3 judges successfully play Episode A MP3 within 48 hours from the file sent directly via Slack/email/Drive

## Reviewer concerns

### 1. SSE event schema has gaps (severity: HIGH) — A-Completeness

Master's SSE list has three gaps that break component integration:

**Fix:** add/extend three events as shown in the schema section above.
- `phase: "internal" | "external"` on all `agent.pitching.*` events so UI distinguishes rounds
- `audio.segment.delayed {segment_index, eta_ms}` for P13 backpressure (tells music filler to fade back in)
- `payment.mocked {frozen_tx_hash, basescan_url, reason, mode_badge}` and `payment.pending {tx_hash, basescan_url, mode_badge}` for payment timeout paths

These are the glue between components; without them, integration will be debugged in the dark on Day 4-5.

### 2. Day 3 overloaded (severity: HIGH) — A-Feasibility

Master's Day 3 stacks: SSE subscriber + pitch-round UI + full Huxe player + music filler + P13 pipelined queue + gapless crossfades. That's two days of work.

**Fix (coordinated across components):**
- `api-storage` Day 3: SSE streaming from `/generate` (real events from real agents)
- `player` Day 3: SSE consumer + pitch-round view + basic audio + ⟲/⏭ + music filler
- `audio` Day 3 morning: P13 client pipelining (moved from Day 2)
- `player` Day 4 morning: client queue + gapless transitions + long-press

### 3. P4 framing contradiction (RESOLVED 2026-04-14)

Earlier version distributed Episode A via a 7-day Supabase signed URL, which contradicted P4's "localhost-only" claim (a signed URL is a public-internet surface). Now resolved: the judge panel is small and internal, so Episode A is handed off as a direct file attachment (Slack/email/Drive). No signed URL, no public bucket exposure. Master P4 rewritten to reflect this.

### 4. `/react` endpoint user validation (severity: low, derived) — not from reviewers

Hackathon runs as single-user `demo-user`. Without validation, a stray frontend bug could write signals to the wrong `user_id` and pollute memory.

**Fix:** `/react` rejects any `user_id != "demo-user"` with 403. Hard-code in v0. Swap for real auth in v1.

### 5. `/generate` idempotency (severity: low, derived)

Multiple rehearsal runs should not collide.

**Fix:** `episode_id = crypto.randomUUID()` on every request. Each run gets a fresh row and a fresh `./data/episodes/{episode_id}/` directory. No overwrites.

## Open questions (component-scoped)

- **SSE reconnection semantics:** if the browser drops mid-generation (wifi hiccup on demo day), does the server hold state so the client can reconnect and replay? v0: no. If demo-day wifi flakes mid-SSE, refresh + re-run. Document in rehearsal checklist.
- **Disk cleanup policy:** each rehearsal generates a new `./data/episodes/{uuid}/` directory. After dozens of rehearsals, the dir bloats. v0: accept it; manual `rm -rf ./data/episodes/` between sessions if needed. v1: add a retention policy (keep last 5 episodes on disk).
- **Zoom audio share:** "Share Sound" must be on when screen-sharing the demo. Pre-demo checklist item (coordinate with `audio/docs`).

# Agent: `youtube_agent`

**Status:** DRAFT — PARTIAL SPEC (in-progress; decisions through 2026-04-14 brainstorming + empirical probe)
**Parent component doc:** [`../../docs/DESIGN.md`](../../docs/DESIGN.md) — `agents` component (shared `DataAgent` protocol, memory shape, `Pitch` shape)
**Master doc:** [`~/.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md`](../../../../.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md)
**Scope:** demo (v0) + v1 onboarding skeleton. Per brainstorming decision — v0 is v1's simplified implementation, not a disposable shortcut.
**Covers:** both the **internal `youtube_agent`** (user-selected) and the **shared YouTube interest-extraction pipeline** reused by `alices_agent` (external). Alice's agent is also a YouTube-seeded agent; duplicating the extraction pipeline would be an architectural smell.

## Purpose

Pitch 3–5 ranked topics to the Producer derived from the user's YouTube world (or, when both signal windows are empty, exactly **1 thin-signal pitch** — never any other cardinality; see §`pitch()` flow → Output contract). The agent owns:

1. **Interest profile extraction** — ingest the user's YouTube signals (subscriptions + liked videos) via YouTube Data API v3, produce a recency-aware topic-scored `InterestProfile` (long-term + recent topic distributions) with channel/video provenance preserved for pitch-time grounding.
2. **Pitch generation** — read the `InterestProfile` + `AgentMemory` + `Brief`, emit ranked `Pitch` objects.

**Signal-derived memory is read-only from this agent.** `topic_multiplier` writes (signal ingestion, update rules, session-end batching) live entirely in the `learning-loop` component — see [§Memory boundary](#memory-boundary-decided-2026-04-15) below. `DataAgent.observe()` is **dropped** from the protocol as of 2026-04-15 — learning-loop consumes `EpisodeSignals` directly; see §`AgentMemory` schema for the locked contract.

**Learning-loop is stubbed in v0 (2026-04-18).** No `/react` ingestion, no session-end writes. `topic_multiplier` stays at its bootstrap default `{}` for the entire v0 demo, so `pitch()`'s `combined_topic_scores[T] * topic_multiplier.get(T, 1.0)` read collapses to `combined_topic_scores[T]` (the multiplier term is identity). Demos that need a non-identity `topic_multiplier` (e.g., to show Episode B's topic re-ranking) seed it via `learning_loop.seed_topic_multiplier(user_id, "youtube", {...})` before generation. See `learning_loop/docs/DESIGN.md` §v0 stub contract.

This is the most design-heavy of the four agents: calendar and weather are structured-input → pitch; `alices_agent` is hand-curated content. YouTube is the only one where "what does a good interest profile look like?" is itself an open question.

## Two-layer architecture (decided, revised 2026-04-15 for write-through)

`AgentMemory` is the single persisted state container for the (user, agent) pair. It hosts two co-located fields written by two different owners and read together by `pitch()`:

- **`memory.profile_state`** — an `InterestProfile` (topic-scored, with channel/video provenance per topic). Written by `agents/youtube`'s `fetch_context()` via **write-through**: on every generation, attempt a live YouTube fetch + extractor pass; on success overwrite `memory.profile_state`; on failure skip the write and leave the previous value in place. The fetch **blocks episode generation** (Player streams a default music bed during the wait, see §Player coordination TBD) with a **15-second total deadline** per call; per-call timeouts and per-error-class retry/refresh policy are specified in §`fetch_context()` failure handling. Represents "what topics does this user care about on YouTube, long-term and recently, with evidence."
- **`memory.topic_multiplier`** — `dict[str, float]`. Written by `learning-loop` at session-end using deterministic update rules over `/react` signals. Represents "how have in-app reactions updated our beliefs."

Both are read by `pitch()`. They are orthogonal update streams — profile reflects off-platform taste, multiplier reflects in-platform feedback — but co-located in one persisted record so (a) `pitch()` does a single memory read, and (b) a failed YouTube fetch gracefully falls back to the last-written profile without any separate cache layer or fallback branch.

`ScopeContext` (per `DataAgent` protocol) carries the current `memory.profile_state` to `pitch()` as `profile`; `AgentMemory` is passed alongside for `topic_multiplier`. The schema for `AgentMemory` is defined in §`AgentMemory` schema (locked 2026-04-15).

## Data source (decided 2026-04-14 after empirical probe)

### Architectural split: acquisition vs. extraction

The data pipeline has two independent concerns, and separating them keeps the extractor portable across acquisition methods:

```
  ACQUISITION LAYER           EXTRACTION LAYER
  ┌──────────────────────┐     ┌──────────────────────┐
  │ Fetch user's subs +  │ →   │ Build InterestProfile │ → InterestProfile
  │ liked videos from    │     │ from API responses    │
  │ YouTube Data API v3  │     │ (pure function)       │
  └──────────────────────┘     └──────────────────────┘
     (variable in v1+)             (stable)
```

The extraction layer is a **pure function**: takes structured API responses (subs list, likes list, topic tags) and returns an `InterestProfile`. It has no knowledge of how the data was acquired. The acquisition layer is currently YouTube Data API v3 and is expected to stay that way for v0 + v1 in all non-EU regions. Alternative acquisition paths (Takeout upload, DPAPI) remain designable future extensions but are not built.

### Acquisition strategy: YouTube Data API v3 (user OAuth, `youtube.readonly`)

**One OAuth click → all data immediately, no archive generation, no upload.** Decision driver: the Data Portability API is unavailable in the US + most non-EU regions, and asking real users to upload a Google Takeout archive is a non-starter for consumer onboarding. YouTube Data API is the only globally-available, zero-friction path.

**The key tradeoff we're eating:** no watch history. The YouTube Data API permanently cannot return watch history (endpoint deprecated 2016, never coming back). We compensate with:

- **Subscribe-date timestamps** from `subscriptions.list` (long-term + "subscribed recently" signal)
- **Like-added-at timestamps** from the `LL` liked-videos playlist via `playlistItems.list` (real recency signal from explicit positive actions)
- **Explicit per-episode learning** via the `/react` signal loop (P11) — the master's learning-loop beat compensates for thin cold-start over time

### Probe results (2026-04-14, dev's own account, `tmp/ydata/probe_1776208130/`)

| Endpoint                                                       | Result                                                                                                 | Notes                                                                                                      |
| -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| `subscriptions.list?mine=true&part=snippet,contentDetails`     | **96 subs**, `snippet.publishedAt` = subscribe date (2016→2026)                                        | Real temporal signal. One quota unit per page of 50.                                                       |
| `playlistItems.list?playlistId=LL&part=snippet,contentDetails` | **77 likes** across **72 unique channels**, `snippet.publishedAt` = added-to-playlist time (2017→2026) | **LL playlist is accessible for own account** (contradicts some 2024 forum chatter). Real like timestamps. |
| `playlistItems.list?playlistId=WL`                             | 0 items                                                                                                | Watch Later dead since Sep 2016. Do not query.                                                             |
| `playlistItems.list?playlistId=HL`                             | 0 items                                                                                                | Watch History dead since 2016. Do not query.                                                               |
| `activities.list?mine=true`                                    | 60 items, only types `playlistItem` + `upload`. No `like` / `subscription` / `favorite` activities.    | **Not useful for interest profiling.** Drop from plan.                                                     |
| `channels.list?id=<20 subs>&part=topicDetails`                 | **100% coverage**, Wikipedia URLs                                                                      | No LLM fallback needed for topic tagging on subscribed channels.                                           |

**Critical empirical finding — likes ∩ subs divergence:**

- 96 subs, 72 unique liked-video channels, **only 18 overlap**
- **49 liked-video channels are NOT subscribed**
- Likes reveal "drive-by interest" in channels the user consumes without committing. If we profiled off subs alone, we'd miss ~68% of the user's liked-content channel surface.

Implication for profile construction: **liked-video channels are first-class profile entities**, alongside subscribed channels. The profile pulls entities from the union, not just subscriptions.

### Endpoints consumed

| Call                                                                               | Purpose                                                             | Quota / call |
| ---------------------------------------------------------------------------------- | ------------------------------------------------------------------- | ------------ |
| `channels.list?mine=true&part=contentDetails`                                      | Resolve current user's `relatedPlaylists.likes` ID (usually `LL`)   | 1            |
| `subscriptions.list?mine=true&part=snippet,contentDetails` (paginated, 50/page)    | Subscriptions with subscribe dates                                  | 1 per page   |
| `playlistItems.list?playlistId={likes_id}&part=snippet,contentDetails` (paginated) | Liked videos with channel attribution + like timestamps + video_ids | 1 per page   |
| `channels.list?id=<batch of up to 50>&part=topicDetails,snippet` (server API key)  | Per-channel `topicCategories` for subscribed channels               | 1 per batch  |
| `videos.list?id=<batch of up to 50>&part=topicDetails,snippet` (server API key)    | Per-video `topicDetails` for liked videos (drive-by genre signal)   | 1 per batch  |

**Typical cost per user:** ~6 calls (subs page + likes page + 2–3 channel-topic batches + 1–2 video-topic batches) ≈ 6 quota units per profile refresh. Default daily quota is 10,000 units → ~1,500 refreshes/day per project. Not a bottleneck.

**Not queried in v0 (confirmed-dead or low-signal):**

- `playlistItems.list?playlistId=WL` — dead
- `playlistItems.list?playlistId=HL` — dead
- `activities.list?mine=true` — returns only uploads + playlist adds, no user-taste activity types
- Custom user-created playlists — possible v1 fast-follow if they carry taste signal

## Shared extractor: `youtube_agent` + `alices_agent`

Both agents are YouTube-interest-profile-based. They share a pure extraction function:

```
agents/youtube/extractor.py       # (subs, likes, channel_topics, video_topics, now) → InterestProfile
agents/youtube/agent.py            # internal user agent (calls YouTube API live, then extractor)
agents/alices/agent.py           # external creator agent (loads Alice's Day-0 JSON, then extractor)
```

**What's shared** — the `extract_profile()` function: pure, takes parsed-API records + per-entity topic dicts + `now`, returns `InterestProfile`. No side effects. No OAuth coupling. No agent coupling. No API calls (callers do acquisition; extractor just transforms).

**What's not shared** — each agent owns its own `pitch()`, per-agent memory, persona/voice metadata, and (for `alices_agent`) wallet + content pack. Each agent owns its own acquisition:

- `youtube_agent` acquires via live YouTube Data API OAuth on the user's account.
- `alices_agent` acquires via a **one-time Day-0 probe** against Alice's own account, with the resulting JSON responses checked into the repo as his static profile input. Alice never re-authenticates — his profile is frozen at Day 0 (with manual refreshes if his taste evolves between demo runs).

**Implication for this spec:** extraction sections below apply to both instances. `pitch()` and memory sections are specific to the internal user agent; `alices_agent`'s specifics (wallet, content pack, persona) live in `agents/alices/docs/DESIGN.md` (TBD).

## `InterestProfile` schema (decided 2026-04-14 — topic-as-entity pivot)

```python
class Contributor(TypedDict):
    kind: Literal["sub", "like"]
    channel_name: str                   # for LLM reading ("Anjunadeep")
    channel_id: str                     # stable YouTube channel ID
    subscribed_at: str | None           # ISO; present iff kind == "sub"
    liked_at: str | None                # ISO; present iff kind == "like"
    video_title: str | None             # present iff kind == "like"
    video_id: str | None                # present iff kind == "like"

class InterestProfile(TypedDict):
    long_term_topic_scores: dict[str, float]    # L1-normalized; {} when no subs
    recent_topic_scores:    dict[str, float]    # L1-normalized; {} when no likes
    combined_topic_scores:  dict[str, float]    # pre-blended ranking score; see §Blend
    topic_provenance:       dict[str, list[Contributor]]   # ≤5 per topic; see §Provenance
    computed_at:            str                 # ISO 8601; profile is recomputed per-generation
    stats:                  dict                # see §Stats below
```

**Topics are the only scored dimension.** Channels and videos live in `topic_provenance` as evidence for each topic, not as scored entities. The YouTube agent pitches topic-based segments, so ranking at the topic level is the natural consumer-aligned shape. Channel identity (name, subscribe date, liked-video titles) is preserved losslessly inside provenance for pitch-time LLM grounding.

**Empty signal = empty dict.** No sentinel values. A user with zero subs has `long_term_topic_scores = {}`; a user with zero likes has `recent_topic_scores = {}`. Downstream (`pitch()`) reads `stats.total_recent_weight` / `stats.total_subs` to calibrate confidence on thin signal.

**Two L1-normalized windows on the same scale.** Long-term (from subs, flat weights) and recent (from likes, decayed weights) are each L1-normalized independently — each non-empty window sums to 1.0. This enables **temporal comparison** directly: `recent["jazz"] > long_term["jazz"]` means "jazz is a bigger share of the user's recent attention than their long-term attention," a unit-consistent claim `pitch()` can act on. Without normalization the raw magnitudes are incommensurable (flat sub counts vs. decayed like weights — different currencies); temporal comparison would be impossible.

**Shared provenance across windows.** One `topic_provenance` dict keyed by topic; `Contributor.kind` discriminates `sub` vs. `like`. A topic appearing in both windows (e.g., subscribed channels AND recent likes both tagged `jazz`) has contributors from both kinds in one combined list. No duplication; no split per-window dict.

### Stats

```python
stats: {
  "total_subs":            int,    # raw count of subscriptions ingested
  "total_likes":           int,    # raw count of liked videos ingested
  "total_recent_weight":   float,  # sum of decayed like weights — confidence signal for recent window
  "unique_topics":         int,    # cardinality of (long_term_topic_scores ∪ recent_topic_scores)
  "tag_coverage_pct":      float,  # 0–100 scale; % of entities (subs + videos) that returned non-empty topicDetails
  "avg_topics_per_entity": float,  # mean topic-list length — high values flag broad-term pollution
}
```

`total_recent_weight` is the key confidence signal for the recent window. A user with 3 old likes will have `total_recent_weight ≈ 0.1`; a user with 20 fresh likes will have `total_recent_weight ≈ 15`. The L1-normalized scores inside the window are still honest distributions of observable behavior, but the consumer can see how much signal underlies that distribution and down-weight claims accordingly.

## `AgentMemory` schema (locked 2026-04-15)

`AgentMemory` is the persisted per-(user, agent) state record. It is the coupling contract between `agents/youtube` (and other topic-scored agents) and `learning_loop`. This section is the canonical definition; `learning_loop/docs/DESIGN.md` references this shape as the locked contract (component stubbed in v0 — see `learning_loop/docs/DESIGN.md` §v0 stub contract).

### Shape

```python
class AgentMemory(TypedDict):
    schema_version:    int                  # = 1 for v0
    profile_state:     InterestProfile      # owned by agents/youtube
    topic_multiplier:  dict[str, float]     # owned by learning-loop
    updated_at:        str                  # ISO 8601; bumped on any field write
```

**`topic_multiplier` stays `dict[str, float]`** — not a richer audit-trail shape such as `list[(topic, multiplier, source_episode, timestamp)]`. Reasons:

- **Memory is current state, not history.** `pitch()` reads a single current multiplier per topic. A richer shape forces pitch() to fold history down to the latest value on every read, pushing learning-loop's internal structure into an agent's read path.
- **Audit lives in the signals log, not memory.** Learning-loop owns a `signals` table (see `learning_loop/docs/DESIGN.md` §When unstubbed) that preserves every `/react` event when unstubbed. Any "why did jazz move from 1.1× to 0.85×" question is answered by joining that log with `EpisodeSignals.emissions` — one lookup on the authoritative source, not a duplicated copy in memory.
- **Session-end batching collapses history.** Multiple `/react` events on one topic within one session produce one aggregated delta per session. There is no natural "one entry per update" to preserve in a list without either losing the batching semantics or re-deriving them at read-time.
- **Schema stability.** Flat `dict[str, float]` is the minimum shape learning-loop needs to write and agents need to read. Future richer structures (per-episode attribution snapshots, versioned history) can land as **new additive fields** on `AgentMemory` without rewriting this one.

`schema_version` is carried so future breaking changes (e.g., splitting `topic_multiplier` into current + history, or adding a second agent-owned signal-derived field) carry a cheap migration signal. v0 pins `schema_version = 1`.

### Field ownership

| Field              | Owner            | Write trigger                                                      | Read by                          |
| ------------------ | ---------------- | ------------------------------------------------------------------ | -------------------------------- |
| `schema_version`   | api-storage      | Row creation; never mutated in v0                                  | both components, migration gates |
| `profile_state`    | `agents/youtube` | `fetch_context()`, iff live YouTube fetch + extractor both succeed | `pitch()`                        |
| `topic_multiplier` | `learning-loop`  | Session-end batched update over collected `/react` signals         | `pitch()`                        |
| `updated_at`       | both             | On any field write                                                 | debug / observability            |

Writers **never reach into the other's fields.** A code-review rule is sufficient in v0; formalization (per-field update policies, Supabase row-level policies) is a v1+ concern.

### Cross-field invariants (v0)

- **Multiplier default on miss.** `pitch()` reads `memory.topic_multiplier.get(T, 1.0)`. An absent key means "no signal, use raw profile score." Required for cold-start and for any topic that appears in the profile but has never been pitched.
- **Multiplier keys ⊆ `profile_state.combined_topic_scores` keys (v0 invariant).** Holds because (a) multiplier writes only happen via `/react` signals on pitched topics, (b) pitched topics are constrained to candidates from `combined_topic_scores` (§`pitch()` flow), and (c) the v0 demo's short window gives YouTube data no time to drift a previously-pitched topic out of `combined`. v1 preserves this structurally via predefined-topics in `combined` (§V1+ open questions). The `.get(T, 1.0)` default means a future violation is silent (multiplier entry simply ignored), not crashing — the invariant is an expected property, not an asserted contract.
- **Multiplier value range: `topic_multiplier[T] ∈ [0.1, 10.0]`.** Learning-loop clamps on write; `pitch()` does not re-clamp on read. `1.0` is the unit element so missing keys compose as identity. Zero / negative values are disallowed — they collapse ranking and make a demoted topic indistinguishable from a thin-signal pitch.
- **Fetch failure is a no-op write.** On API-fetch failure, `fetch_context()` skips the `profile_state` write; the prior value is reused. First-ever episode with failure → `profile_state` is an empty `InterestProfile` (§`InterestProfile` schema) and `pitch()` degrades to thin-signal handling (§`pitch()` flow).
- **`updated_at` is advisory.** Bumped on any field write. Never branched on by `pitch()` or learning-loop; staleness does not affect logic. Consumers: debug views, the `memory.update.applied` SSE beat.
- **Neither writer blocks the other.** `fetch_context()` and learning-loop's session-end update modify disjoint fields; the only concurrent-write risk is last-write-wins on `updated_at`. v0 is single-user, single-process, sequential (§`fetch_context()` failure handling — "v0 multi-writer note"), so the risk is null. v1+ adds per-(`user_id`, `agent_id`) CAS on `updated_at`.

**Why co-located rather than separate tables:** orthogonal update streams but co-consumed at pitch-time. One record = one read, one persisted blob, one source of truth for "what does this agent know about this user." Separate tables would need a join on every pitch and a cross-table consistency story for no benefit.

### `EpisodeSignals` companion schema

Learning-loop consumes session-level `EpisodeSignals` at session-end and derives its `topic_multiplier` writes from there. The shape is pinned here so Producer, Player, and learning-loop agree on emission.

```python
class PitchEmission(TypedDict):
    segment_index:   int                  # position in the final running order
    agent:           str                  # "youtube", "calendar", "weather", "alices"
    topic:           str | None           # None for agents that don't do topic-level pitching
    source_refs:     list[str]            # human-readable channel_name / video_title strings, as emitted on the Pitch (for downstream LLM grounding)
    priority:        float                # the priority at which this pitch entered the running order

class ReactionEvent(TypedDict):
    type:                   Literal["like", "skip", "replay"]
    segment_index:          int           # foreign key into EpisodeSignals.emissions[*].segment_index
    timestamp_ms:           int
    segment_position_sec:   float         # playhead before mutation; see learning_loop/docs/DESIGN.md Reviewer Concern #4

class EpisodeSignals(TypedDict):
    schema_version:  int                  # = 1 for v0
    episode_id:      str
    user_id:         str
    emissions:       list[PitchEmission]  # every pitch in the running order; written at episode start
    reactions:       list[ReactionEvent]  # zero or more per episode; may be empty for silent sessions
```

**Why `emissions` is carried explicitly.** Learning-loop updating `topic_multiplier[T]` requires knowing `T` was pitched — and at what priority, if update rules want to weight the delta. The running order is built by Producer and consumed by Player; neither holds it after episode-end unless it's logged. Bundling `emissions` into `EpisodeSignals` means learning-loop has everything it needs in one record, without querying Producer's state post-hoc.

**Why reactions reference `segment_index`, not `topic`.** The reaction happens on a playing segment, not a topic. The segment→topic mapping lives in `emissions` (one hop). Player's emission stays trivial (timestamp + segment index + type); the "which topic got skipped" inference is localized to learning-loop, where it belongs.

**Emission protocol.** At episode start (before first segment plays), Producer writes `EpisodeSignals{episode_id, user_id, emissions: [...], reactions: []}` to api-storage. Player appends `ReactionEvent` rows via `/react` as they happen. At session-end, learning-loop reads the complete record and writes `topic_multiplier` for any topic-scored agent whose pitches appear in `emissions`.

**Topic-less agents.** `weather_agent` pitches "it's going to snow" — no topic. `topic: None` in its `PitchEmission`. Learning-loop does not update `topic_multiplier` for that segment (the field is topic-keyed). Agents whose memory shape isn't topic-keyed at all live outside this contract and are designed in their own docs.

### Bootstrap defaults

On `load_memory(user_id, agent_name)` for a pair that has never had a row:

```python
AgentMemory(
    schema_version   = 1,
    profile_state    = InterestProfile(
        long_term_topic_scores = {},
        recent_topic_scores    = {},
        combined_topic_scores  = {},
        topic_provenance       = {},
        computed_at            = now_iso(),
        stats = {
            "total_subs":            0,
            "total_likes":           0,
            "total_recent_weight":   0.0,
            "unique_topics":         0,
            "tag_coverage_pct":      0.0,
            "avg_topics_per_entity": 0.0,
        },
    ),
    topic_multiplier = {},
    updated_at       = now_iso(),
)
```

**Lazy creation.** api-storage returns the default above when no row exists for `(user_id, agent_name)`; the row is persisted on the first real write (either `fetch_context()` success or learning-loop's first session-end update). No coordinated signup fan-out across agents.

**First-episode behavior chain:**

1. `fetch_context()` runs. On success: `profile_state` overwritten with a real `InterestProfile`. On failure: empty `InterestProfile` remains in place.
2. `pitch()` reads `profile_state.combined_topic_scores`. If empty (fetch failure or zero-subs/zero-likes user): emit exactly 1 thin-signal `Pitch` (§`pitch()` flow output contract).
3. `topic_multiplier == {}` → `pitch()`'s `.get(T, 1.0)` default makes this identical to "all topics at neutral multiplier." No cold-start branching required inside `pitch()`.
4. At session-end, learning-loop applies reactions (if any) → first non-empty `topic_multiplier` snapshot persists. **v0 stub:** this step does not fire in v0. `topic_multiplier` stays `{}` across episodes unless fixture-seeded.

## Aggregation: TF-IDF with sublinear TF and L1 normalization (decided 2026-04-14, IDF scope revised 2026-04-15)

Topic scores use TF-IDF, computed independently per window (IDF included — see below), with sublinear TF scaling and L1 normalization per window.

**All `log` references in this section are natural log (Python `math.log`).**

**Document set for IDF — per window (revised 2026-04-15).** A "document" is a tagged entity. Long-term IDF treats each subscribed channel as one doc (`N_long = len(subs)`); recent IDF treats each liked video as one doc (`N_recent = len(likes)`). For topic `T` in a given window, `df_window(T)` = count of entities in *that* window whose topic list contains `T`. IDF is computed independently per window:

```
idf_long(T)   = log((N_long   + 1) / df_long(T))
idf_recent(T) = log((N_recent + 1) / df_recent(T))
```

Empty windows (`N_window = 0`) skip IDF computation; the window returns `{}` per the empty-signal rule in §`InterestProfile` schema.

**Numerator-only Laplace smoothing.** Without smoothing, when a topic tags every entity in a window (`df = N`) — e.g., a 3-sub user all tagged `music` — `log(N/df) = log(1) = 0` zeros out that topic and the user ends up with no scored profile despite clearly-observable taste. The chosen smoothing `log((N+1)/df)` is **asymmetric** — numerator only, not the symmetric textbook Laplace `log((N+1)/(df+1))`. The asymmetry preserves a stronger gap between rare and common terms at the low N that dominates v0 (dev's 96 subs / 77 likes is small by IR standards). Ordering between informative and uninformative topics is unchanged at meaningful N (lifestyle 7/20 vs. rare 1/20: ratio 0.35 → 0.36). A textbook-IDF auditor will flag the asymmetry; we accept that as a deliberate Laplace variant.

**Term frequency, per window:**

- **Long-term TF.** For each subscribed channel `C`, for each topic `T` in `channel_topics[C.channel_id]`:

  ```
  tf_long[T] += 1.0
  ```

  Subscribing is a durable commitment and carries flat weight regardless of age. Age is expressed in provenance via `Contributor.subscribed_at`, not by decaying the TF signal.

- **Recent TF.** For each liked video `V` with like timestamp `liked_at`, compute `decayed_weight = exp(-(now - liked_at).days / 90)`. For each topic `T` in `video_topics[V.video_id]`:
  ```
  tf_recent[T] += decayed_weight
  ```

**Sublinear TF scaling (revised 2026-04-15).** After raw TF accumulation, apply per window:

```
tf[T] := log(1 + tf[T])
```

Smooth, non-negative for all `tf ≥ 0`, monotone, no conditional needed. Prevents a single dominant source from swamping a topic's score — e.g., one channel with 20 likes shouldn't contribute 20× another channel's single like.

**Why `log(1 + tf)` instead of the more common `1 + log(tf)`:** the recent window's TF is a sum of decayed weights, often fractional (e.g., one 270-day-old like has `decayed_weight ≈ 0.125`). The naive `1 + log(0.125) = -1.08` produces *negative* TF, breaks L1 normalization (sum can flip sign or vanish), and silently corrupts every recent score on aged-like data — precisely dev's own probe shape (77 likes spread over 8 years). `log(1 + tf)` is well-defined and non-negative for all `tf ≥ 0`, integer or fractional.

**Score = TF × IDF (per-window), then L1-normalize per window:**

```
score[window][T] = tf_sublinear[window][T] × idf_window(T)
score[window]    := score[window] / sum(score[window].values())   # L1
```

Empty windows (raw TF dict has no keys) skip normalization and return `{}`.

**Why this handles broad-term pollution without extra filters.** In the long-term window, `lifestyle` appearing on 7 of dev's first 20 probed subs has `idf_long ≈ log(21/7) ≈ 1.10`. A rare genre on 1/20 has `idf_long ≈ log(21/1) ≈ 3.04`. Pervasive tags get ~3× penalized against rare ones automatically — no separate "drop topics appearing on >X% of channels" rule needed. Same logic applies independently inside the recent window with its own IDF.

**Why per-window IDF (revised 2026-04-15 from shared user-IDF).** Earlier draft computed one IDF over `subs ∪ likes`. The flaw: a user with 96 subs (none `jazz`) and **10 jazz likes** vs. another with 96 subs (none `jazz`) and **2 jazz likes** — same user-level narrative ("jazz is their recent thing") — yields *lower* IDF for the active liker (`log(107/10) ≈ 2.37`) than the casual one (`log(99/2) ≈ 3.90`). More evidence → less informativeness, exactly backwards from intent. Per-window IDF answers "how informative is this topic *within this evidence base*" — the recent window's IDF doesn't dilute when a user racks up jazz likes; it stays self-consistent as a recent-attention measure. The L1 normalization per window already keeps cross-window comparison unit-consistent ("`recent['jazz'] > long_term['jazz']`" = "jazz is a bigger share of recent than long-term attention"); per-window IDF keeps the *within*-window comparisons honest too.

**Why no hard-cap on topic count.** After TF-IDF + L1 the long tail of rarely-relevant topics naturally shrinks to small fractions. `pitch()` reads the dict as-is and makes its own top-N selection at prompt-assembly time. Profile stays honest about the distribution shape.

## Provenance and K=5 compression (decided 2026-04-14)

`topic_provenance[T]` is capped at **K=5 contributors per topic**, constructed at profile-build time so the profile is drop-in LLM context without downstream filtering.

**Selection rule per topic T:**

1. Gather all subscribed channels whose topics include `T` → sort by `subscribed_at` **ascending** (oldest first — ingrained taste reads first). Take first 2.
2. Gather all liked videos whose topics include `T` → sort by `liked_at` **descending** (most recent first). Take first 3.
3. Concatenate (subs block, then likes block). If one side is short, fill from the **other side continuing in the same sort order** — e.g., subs side short → take next-newest likes (positions 4, 5, ... in `liked_at` desc); likes side short → take next-oldest subs (positions 3, 4, ... in `subscribed_at` asc). Up to K=5 total. (Determinism note 2026-04-15: continuing in the same sort order, rather than switching direction or sampling, makes contributor selection reproducible for unit tests.)

**Why this order.** LLMs consuming structured lists tend to give weight to early entries. Subs-first-then-likes surfaces durable-taste grounding ("you've been subscribed to Anjunadeep since 2019") before recent-attention grounding ("and you liked this Mr. Suicide Sheep track last month"), producing natural narrative voice at pitch-time.

**Why K=5 with 2/3 split.** Every topic shows both "durable" and "fresh" evidence when both exist. Fixed size = predictable prompt budget. At scale: 50 topics × 5 contributors ≈ 250 contributor entries per profile, vs. thousands without compression.

**What's lost.** The long tail of contributors per topic. Acceptable because:

- Long-tail is dominated by broad-tag entities (a 10th `lifestyle`-tagged channel adds little beyond the 5th).
- API responses are cached; a debug view can recompute the full list on demand.
- The score itself (TF×IDF) reflects the full long-tail — compression affects only provenance, not ranking.

**K is tunable; revisit when hook-quality data exists (2026-04-15).** K=5 and the 2/3 split are provisional — chosen to give the LLM enough provenance for grounded hooks without bloating prompt budget, but the right K depends on hook-hallucination measurements that don't exist yet. Once a hook-fidelity rubric and real-data hooks-to-grade exist, revisit both K and the split. The asymmetric case (sub-only or like-only topics, where the "durable then fresh" narrative collapses to "5 of one kind") is also part of that revisit — the LLM prompt template will need a `provenance_shape` branch and the right shape is empirical, not first-principles. Tracked for the prompting-pipeline design session (separate scope).

## Recency decay (decided 2026-04-14)

Like timestamps decay exponentially with a **90-day half-life**, no hard cutoff. The decayed weight feeds into recent TF (§Aggregation).

```python
RECENT_HALF_LIFE_DAYS = 90

def decayed_weight(liked_at: datetime, now: datetime) -> float:
    # Fractional days for sub-day precision; max(0, ...) clamps clock-skew /
    # timezone artifacts where liked_at > now would yield negative age and
    # weight > 1.0, violating the [0, 1] weight invariant assumed by the α-blend
    # and the total_recent_weight confidence signal.
    age_days = max(0.0, (now - liked_at).total_seconds() / 86400.0)
    return math.exp(-age_days * math.log(2) / RECENT_HALF_LIFE_DAYS)
```

**Weights at a glance:** 30d → 0.79, 60d → 0.63, 90d → 0.50, 180d → 0.25, 1y → 0.06, 8y → ~0.

**Formula note (2026-04-15):** the `log(2)` factor makes `RECENT_HALF_LIFE_DAYS` a _true_ half-life — at `age = 90d`, weight is exactly 0.5, matching the prose and the table above. An earlier draft used `exp(-age/90)` which is a time-constant (90d → 0.37), and silently mismatched the "half-life" label.

**Why exponential (vs. piecewise buckets or hard cutoff):** LLM consumers are insensitive to score-curve smoothness, so bucket cliff-effects carry no cost. Exponential wins on (a) one knob is easier to tune than tier boundaries, and (b) no hard cutoff needed — 8-year-old likes decay to ~0 naturally.

**Why 90-day half-life (vs. 30 / 60 / 180):** empirically chosen against dev's sparse signal (77 likes / 8 years ≈ 10/year). Shorter half-lives leave `recent` near-empty; 180d blurs "recent" into long-term. 90d produces meaningful shape for pitch-time reasoning without diluting the recency signal.

**Why only likes decay, not subs:** subscribing is a durable commitment — old subs have _survived_ years of possible unsubscribes, so age makes the signal _stronger_, not weaker. Subscribe dates are preserved in provenance (`Contributor.subscribed_at`) for LLM-time temporal reasoning, not baked into a decayed score. Liking is a moment-in-time event, so decay applies.

**Why one half-life (not per-signal):** we consume one decaying signal type (likes). Per-signal half-lives are only useful when differentiating plays / skips / saves / follows — YAGNI for v0.

**Tunable:** `RECENT_HALF_LIFE_DAYS = 90` is the single knob. Revisit when multi-user data arrives.

## Blend: `combined_topic_scores` (decided 2026-04-15)

The two L1-normalized windows (`long_term_topic_scores`, `recent_topic_scores`) are fused into a single ranking score per topic, with the mixing ratio driven by how much recent signal actually exists. The blend is precomputed at profile-build time and stored as `combined_topic_scores` so `pitch()` has a drop-in ranking dimension without re-deriving the policy.

**Formula:**

```python
BLEND_HALF_SATURATION_K = 5.0     # tunable; "W at which recent and long-term weigh equally"

W = stats["total_recent_weight"]  # unbounded, ≥ 0
α = W / (W + BLEND_HALF_SATURATION_K)

combined = {}
for T in (long_term_topic_scores.keys() | recent_topic_scores.keys()):
    combined[T] = (1 - α) * long_term.get(T, 0.0) + α * recent.get(T, 0.0)

# Safety-net L1 renormalize (added 2026-04-15). When both windows are non-empty
# the sum is already 1 (convex combination of two L1 distributions on a topic
# union). When one window is empty the sum is α or (1-α), not 1. Renormalize
# unconditionally so the "all topic dicts are L1" invariant holds regardless
# of which window contributed — costs nothing in the both-non-empty case.
s = sum(combined.values())
if s > 0:
    combined = {T: v / s for T, v in combined.items()}
```

`combined` is L1-normalized in all cases by the safety-net step. (Pre-renormalize sums: 1 when both windows non-empty; `α` when long-term empty; `(1-α)` when recent empty; 0 when both empty → returns `{}`.)

**α-curve at a glance (`k=5`):**

| W (`total_recent_weight`) | Signal                | α     | Blend behavior        |
| ------------------------- | --------------------- | ----- | --------------------- |
| 0.1                       | 1 very old like       | 0.02  | essentially long-term |
| 1.0                       | few old likes         | 0.17  | long-term dominant    |
| 5.0                       | ~5 fresh likes        | 0.50  | balanced              |
| 15.0                      | ~15 fresh likes (dev) | 0.75  | recent-favored        |
| 30.0+                     | very active user      | 0.86+ | recent dominant       |

**Why saturating (`W/(W+k)`) over linear or thresholded:** bounded to `[0, 1]` without a cap cliff, single-knob tunable, zero-signal falls out naturally (`W=0 → α=0 → combined = long_term`), and the half-saturation point has an intuitive meaning (`k` = "how many fresh likes until recent carries equal weight").

**Why `k=5`:** empirically matched against dev's probe — `W≈15` for an active user should be recent-dominant (α=0.75) but not total (α<1). Placing `k=5` puts the balanced point at ~5 fresh likes, which is a reasonable "enough to trust" threshold. Tune when multi-user data arrives.

**Why keep raw `long_term_topic_scores` + `recent_topic_scores` alongside `combined_topic_scores`:** the blend compresses the temporal-divergence signal (which topics are _rising_ vs _durable_) that narrative layers want. Pitch-time LLM prompts can still read both windows directly when they need to say "you've been getting into jazz lately" (requires `recent[jazz] >> long_term[jazz]`) — the combined score is for ranking, the raw windows are for narrative.

**Why precompute inside `InterestProfile` rather than at pitch-time:** `α` and `k` are profile-layer policy, not pitch-layer policy. Embedding the blended score in the profile means `pitch()` doesn't re-implement the blend, learning-loop-driven memory reweights apply _on top_ of the blend (not under it), and debug views can inspect `combined` directly.

## `pitch()` flow: algo then LLM (decided 2026-04-15)

`pitch()` is a two-step pipeline: a deterministic algo step assembles candidates, then a bounded LLM call articulates them into `Pitch` objects. Ordering, knobs, and LLM input shape are fixed here so `Pitch` generation doesn't drift across sessions.

```python
def pitch(brief: Brief, memory: AgentMemory, context: ScopeContext,
          user_id: str) -> list[Pitch]:
    profile: InterestProfile = context["profile"]

    # ── Empty-profile thin-signal short-circuit ──
    if not profile["combined_topic_scores"]:
        return [thin_signal_pitch(profile)]   # exactly 1 pitch; see §Output contract

    # ── Step 1: algo — candidate assembly (deterministic) ──
    score = {
        T: profile["combined_topic_scores"][T] * memory["topic_multiplier"].get(T, 1.0)
        for T in profile["combined_topic_scores"]
    }
    # top_n_seeded: returns min(n, len(score)) items; ties resolved by
    # random.Random((user_id, profile["computed_at"])).shuffle for
    # deterministic-per-(user, generation) variety without flapping unit tests.
    candidates = top_n_seeded(score, n=8, seed=(user_id, profile["computed_at"]))
    bundle = [
        {
            "topic": T,
            "score": score[T],
            "long_term": profile["long_term_topic_scores"].get(T, 0.0),
            "recent":    profile["recent_topic_scores"].get(T, 0.0),
            "provenance": profile["topic_provenance"].get(T, []),   # already K=5 capped; absent if no contributors survived filtering
        }
        for T in candidates
    ]

    # ── Sparse-topic guard ──
    # Output contract requires 3–5 pitches or exactly 1 thin-signal.
    # If fewer than 3 candidates survive scoring, the profile is too
    # sparse for ranked topic pitches — emit thin-signal instead.
    if len(bundle) < 3:
        return [thin_signal_pitch(profile)]

    # ── Step 2: LLM — selection + articulation (bounded, no external calls) ──
    return llm.generate_pitches(bundle, brief)   # returns 3–5 Pitch objects
```

**Step 1 — algo, what and why:**

- **Read-only inputs.** `memory` and `profile` are both read, never mutated.
- **Memory as multiplier, not override.** `memory.topic_multiplier[T]` scales the profile-derived score; absent keys default to 1.0. This composes cleanly with the profile blend and keeps the profile honest about raw off-platform taste — memory is a lens on top, not a replacement.
- **Over-select `n=8`.** The final pitch budget is 3–5. Giving the LLM ~2× headroom lets step 2 pick on fit/story rather than be forced to use all candidates. Smaller `n` saves tokens but under-samples the long tail.
- **Both windows in the bundle, plus the combined score.** The algo ranks on `combined`, but the LLM sees `long_term` and `recent` separately so it can detect divergence topics worth narrating ("you've been into jazz lately, here's a fresh drop").

**Step 2 — LLM, what and why:**

- **Input-bounded.** Candidates bundle + brief. **No external search, no web fetch, no agentic loop.** Content discovery (fresh articles, new videos, world news) is Producer's job, not the agent's. The agent knows the user; Producer knows the world. For the full rationale on why this boundary limits hallucination surface, see [`agents/docs/DESIGN.md` §Two-LLM boundary](../../docs/DESIGN.md#two-llm-boundary-why-agents-pitch-and-producer-scripts-decided-2026-04-16).
- **Constraint: pick from the candidate set.** The LLM re-ranks, writes hooks, and selects 3–5, but cannot invent topics outside `bundle`. Otherwise the algo layer is decorative and the deterministic demo beat is undermined.
- **Output contract (clarified 2026-04-15, revised 2026-04-16).** Either **3–5 topic `Pitch` objects** with `title`, `hook`, `priority ∈ [0, 1]`, `source_refs` (human-readable channel names / video titles drawn from the topic's provenance, for downstream Producer LLM grounding — not opaque YouTube IDs), **or exactly 1 thin-signal `Pitch`**. **Never any other cardinality.** Thin-signal is emitted in two cases: (1) `combined_topic_scores` is empty (zero-subs zero-likes user, or first-ever episode where API failure left `profile_state` empty), or (2) fewer than 3 candidates survive the algo step (profile is non-empty but too sparse for ranked topic pitches — e.g., a user with only 1–2 unique topics after TF-IDF). The sparse-topic guard enforces the 3-pitch floor structurally rather than letting the LLM attempt to fill 3 slots from insufficient candidates. Producer is responsible for handling the 1-pitch thin-signal case in its running-order assembly (e.g., let other agents fill the slot, or play a "let me learn about you" segment).

**Why this split:**

- **Demo legibility.** Step 1 is reproducible and testable — Episode B's "shifts" (memory multiplier changes) produce visible candidate-score changes unit tests can lock. Step 2 is where narrative variance lives, scoped to articulation only.
- **Cost/latency.** One scoped LLM call per generation, no external loops. Predictable wall-clock for the live demo.
- **Layer integrity.** LLM is the narrative engine, not the state transition engine — matches the §Memory boundary decision (state transitions = deterministic; reasoning = LLM).

**Brief-driven filtering is out of scope for this step.** Whether "morning briefing" filters music topics out is a Producer-layer concern (priority re-weighting over the pool of all agents' pitches), not a youtube-agent concern. `pitch()` emits its best 3–5 pitches for the user's taste; Producer decides which make the running order given the brief.

**Thin-signal handling.** Without watch history the profile leans on subs (long-term only) + likes (sparse — dev has 77 likes across 8 years). Two thin-signal modes the algo step must tolerate:

- **Sparse `recent_topic_scores`.** `stats.total_recent_weight` surfaces signal richness; the blend's saturating α already collapses the recent window's influence when `W` is small (W=0.1 → α=0.02 ≈ pure long-term). The LLM bundle still includes the (mostly-empty) `recent` field per topic; LLM is instructed to reach for `subscribed_at` from provenance for any recency-adjacent narrative when `recent` is empty.
- **Empty recent window** (zero-likes user, or first-ever episode with API failure leaving `profile_state` empty). `recent_topic_scores = {}`, `combined_topic_scores` reduces to long-term-only. `pitch()` proceeds against `long_term` alone; if `long_term` is also empty (`combined_topic_scores == {}`), the empty-profile short-circuit at the top of `pitch()` emits a single thin-signal `Pitch` rather than guessing — see §Output contract for the cardinality contract. After Episode 1, in-app `/react` signals carry recency weight via `memory.topic_multiplier` regardless — for returning users, thin-profile is self-correcting.

## Memory boundary (decided 2026-04-15, revised for write-through)

`AgentMemory` has **field-level ownership**, not component-level read-only/write-only. `agents/youtube` owns writes to `memory.profile_state` via `fetch_context()`'s write-through. `learning-loop` owns writes to `memory.topic_multiplier` and all signal-derived fields. Neither component reaches into the other's fields. This is still a component-level separation of concerns — it's the boundary that's drawn by _field_, not by _record_:

| Concern                                            | Owner            |
| -------------------------------------------------- | ---------------- |
| `InterestProfile` extraction from YouTube          | `agents/youtube` |
| Writing `memory.profile_state`                     | `agents/youtube` |
| `pitch()` composition (algo + LLM)                 | `agents/youtube` |
| Reading `memory.profile_state` at pitch-time       | `agents/youtube` |
| Reading `memory.topic_multiplier` at pitch-time    | `agents/youtube` |
| `/react` signal ingestion                          | `learning-loop`  |
| Update rule semantics (react → multiplier delta)   | `learning-loop`  |
| Writing `memory.topic_multiplier`                  | `learning-loop`  |
| Session-end batching, move cap, decay, attribution | `learning-loop`  |

**Why deterministic updates (algorithmic, not LLM-driven):**

- Reproducibility: same input signals → same memory state. Demo beat "Episode B shows shifts" is auditable end-to-end.
- Testability: rule table locks in unit tests; drift is caught mechanically.
- Cost/latency: no LLM call per session end, no API failure mode in the learning path.
- Auditability: "jazz moved from 1.1× to 0.85× because: skipped at 0.85×" is one lookup. LLM-driven memory would yield "why did it move? ask the model."

**Parked for v1+ (requires real benchmark):** LLM-assisted memory updates as an A/B against the deterministic baseline. Needs ground truth (user metrics like retention, completion rate) and an eval harness before the comparison is meaningful — without those, "benchmark later" becomes vapor.

**`DataAgent.observe()` is dropped from the protocol (decided 2026-04-15).** Learning-loop consumes `EpisodeSignals` directly from api-storage at session-end (see §`AgentMemory` schema → §`EpisodeSignals` companion schema for the shape). Field ownership forbids agents from writing `topic_multiplier` — an `observe()` that cannot write the only signal-derived field has no job. Centralizing rule application in learning-loop keeps policy in one place rather than scattered across four agent files. `agents/docs/DESIGN.md` `DataAgent` protocol is updated accordingly. If an agent ever needs signal-derived state that is *not* a topic multiplier (e.g., per-channel engagement counts), `observe()` can return as an agent-owned write path on an agent-owned field, not as a learning-loop hook.

## Topic tagging (decided, extended 2026-04-14)

**Strategy:** server-side cache of `channel_id → topic_tags` AND `video_id → topic_tags`, populated from YouTube Data API's `topicCategories` / `topicDetails` fields.

- **Subscribed channels** get channel-level tags via `channels.list?part=topicDetails`.
- **Liked videos** get **per-video** tags via `videos.list?part=topicDetails`. Per-video tagging captures drive-by genre interest within otherwise-themed channels — e.g., a jazz channel's occasional rock cover correctly contributes to `rock-music` only for that like, not to the channel's full topic inheritance.

**Why topicCategories works:**

- Returns a list of Wikipedia URLs (e.g. `https://en.wikipedia.org/wiki/Electronic_music`). Canonical, human-readable, pre-normalized by Wikipedia. No aliasing across users.
- Public metadata → **server API key is sufficient**; no user OAuth required for the tagging step.
- `channels.list` and `videos.list` both accept comma-separated IDs (up to 50), 1 quota unit per call.
- **Empirical coverage: 100%** on the first 20 of dev's subscriptions via `channels.list`. **Per-video coverage: 89.6%** on dev's 77 liked videos via `videos.list` (`tmp/ydata/probe_1776208130/08_video_topic_details.json`, 2026-04-15 probe — 69/77 vs requested, 95.8% vs returned; 5 videos unreturnable, likely deleted/private). Comfortably above the 70% gate — no LLM fallback required for v0. Topic histogram is sensible (Music 33, Pop_music 16, Jazz/Electronic 8 each, Classical/Soul 6/5), validating the drive-by genre thesis (e.g., a short on a non-music channel correctly tagged Pop_music).

**Normalization:**

```
https://en.wikipedia.org/wiki/Rock_music                → "rock-music"
https://en.wikipedia.org/wiki/Lifestyle_(sociology)      → "lifestyle"          # strip parenthetical
https://en.wikipedia.org/wiki/Video_game_culture         → "video-game-culture"
```

Rule: take last path segment, URL-decode, strip parenthetical suffixes `(...)` from the end, convert `_` → `-`, lowercase.

**LLM fallback (deferred to fast-follow):** if per-video coverage drops meaningfully below 70% in future probes (e.g., on Alice's account or on a new test user with different content shape), add a single-pass LLM enrichment: "given video title + channel name, output 1–3 kebab-case topic tags." Not built for v0 — dev-account coverage exceeds the gate.

**Graceful degradation:**

- Coverage drops → profile works at coarser granularity (fewer topics, smaller dicts).
- YouTube API over quota → `channel_id → topics` and `video_id → topics` caches absorb new users; misses contribute empty tag lists (entity makes no contribution to any topic score, stats.tag_coverage_pct drops).
- All topic dicts empty → `pitch()` must tolerate `long_term_topic_scores = {}` and `recent_topic_scores = {}`; falls back to pitching from whatever signal survives (or emits a "thin-signal" notice pitch).

## Auth model summary

| Auth                                  | Used by | Purpose                                                             |
| ------------------------------------- | ------- | ------------------------------------------------------------------- |
| User OAuth `youtube.readonly`         | v0 + v1 | Fetch user's subs + liked videos via YouTube Data API v3            |
| Server API key (developer credential) | v0 + v1 | `channels.list?part=topicDetails` for any channel (public metadata) |

**One consent prompt, one scope, one API, globally available.** No regional gating, no async archive, no upload, no verification gate (`youtube.readonly` is a common non-sensitive scope). **Project scope is internal hackathon, solo-dev** — Google's app verification (paperwork required at >100 users in production) is **out of v0/v1 scope and not on the critical path**. Testing-mode publishing covers the demo audience (dev + Alice + a handful of invited testers, all explicitly added as test users in the OAuth consent screen).

**Demo auth flow (decided 2026-04-16):** YouTube OAuth is triggered inline when the user selects the YouTube agent on the agent selection screen. This is step 1 of the sequential auth flow (YouTube → Calendar → Weather GPS → Alice). The consent popup is visible to the demo audience — judge sees the demonstrator approve YouTube access, establishing the "real agents with real data" thesis before any episode is generated. See `agents/docs/DESIGN.md` §Agent Selection & Auth Sequence.

**Token lifecycle (v0 scope):** one consent at OAuth flow start yields an access token (~1h) + refresh token. Access tokens silently rotate via refresh — no user re-prompt per episode. Relevant only for the 10-min demo window: one consent at session start covers everything. Dev-time caveat: while the OAuth consent screen is in Google's Testing publishing status, refresh tokens expire after 7 days, so the dev account re-consents weekly. Production-scale lifecycle (revocation, multi-device, long-lived storage) is deferred to v1; `youtube.readonly` testing-mode supports several users which covers v0 (dev + Alice + a handful of invited testers), and Google app verification (privacy policy, scope justification, demo video) is only required at the >100-user production threshold.

**Alice's one-time profile:** same probe script run against Alice's account at Day 0, with the resulting JSON responses committed as his static input (not his OAuth token). No live OAuth for Alice's agent at runtime. Consent captured verbally from Alice for demo-day use. **Demo-day prep (2026-04-15):** re-probe Alice's account ~24 hours before showtime — single OAuth re-consent + run probe script + commit fresh JSON to `agents/alices/data/`. Keeps his profile reflective of his current taste (avoids the "frozen-creator vs. live-user" mismatch that would otherwise compound over weeks). If his taste evolves between demos beyond the 24h window, re-run on demand.

## `fetch_context()` failure handling (decided 2026-04-15)

`fetch_context()` is the only network-touching boundary in `agents/youtube`. The "no-op write on failure" rule from §Two-layer architecture is the *outer* envelope — a fetch that ultimately fails leaves `memory.profile_state` untouched and the prior profile is reused. This section specifies the *inner* retry/refresh behavior per error class so failures are handled honestly per type, not silently lumped into one bucket.

**Per-call timeout:** 5 seconds per HTTP request. **Total `fetch_context()` deadline:** 15 seconds (set in §Two-layer architecture). When the total deadline fires, abandon any in-flight requests, skip the write, return the prior `profile_state`. Player keeps streaming the default music bed during the window.

| HTTP status / error reason                              | Class                  | Action                                                                                                                                                       |
| ------------------------------------------------------- | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `401 Unauthorized` (expired access token)               | Recoverable in-session | Refresh access token via stored refresh token, retry the request **once**. If refresh itself returns `invalid_grant`, treat as re-consent needed (row below). |
| `403 quotaExceeded` / `dailyLimitExceeded`              | Recoverable tomorrow   | No retry. Skip write. Log the quota-exhaustion event for ops visibility.                                                                                     |
| `403 rateLimitExceeded` / `userRateLimitExceeded`       | Recoverable shortly    | Exponential backoff (1s, 2s, 4s), retry up to 3 times within the 15s deadline.                                                                               |
| `403 accessNotConfigured` / `forbidden` (scope/API off) | Unrecoverable          | No retry. Fail loud in dev (raise / log error). v1+ surfaces to ops dashboard.                                                                               |
| `404` (`channelNotFound`, `playlistNotFound`, etc.)     | Per-entity miss        | Log + skip the affected entity. Continue the overall fetch. Affected topics simply don't get a contributor from that entity (lowers `tag_coverage_pct`).      |
| `429 Too Many Requests`                                 | Recoverable shortly    | Same as `403 rateLimitExceeded`: backoff + retry up to 3 within deadline.                                                                                    |
| `5xx` (500/502/503/504)                                 | Transient backend      | Exponential backoff (1s, 2s, 4s), retry up to 3 times within deadline.                                                                                       |
| Network timeout (no response within per-call timeout)   | Transient              | Retry once. If still timeout, treat as failure and skip write.                                                                                               |
| `invalid_grant` on refresh                              | Re-consent needed      | Token chain is dead. v0: log + skip write (user keeps prior profile until they manually re-consent). v1+: surface re-consent UI.                             |

**Refresh token expiry caveat (v0 dev mode).** While the OAuth consent screen is in Google's Testing publishing status, refresh tokens expire after **7 days** — so the 401-then-refresh path will get `invalid_grant` instead of a fresh access token after that window. Handled per the `invalid_grant` row above. Demo-day prep includes re-consenting all test users within 24h of showtime (see §Auth model summary, §Build plan).

**Why the matrix matters.** "No-op on failure" is the right outer rule, but treating a recoverable 401 the same as an unrecoverable `accessNotConfigured` would mean a routine token expiry silently degrades a user's profile for an entire session with no signal that anything went wrong. The matrix keeps recoverable paths recovering and unrecoverable paths loud — fail-graceful at the outer envelope, fail-honest in the inner mechanism.

**v0 multi-writer note (parked):** v0 demo is single-user, single-process, sequential user actions (click → wait → listen → react → next), so two `fetch_context()` calls cannot race. Concurrency control (CAS on `updated_at`, per-(user_id, agent_id) lock around `fetch_context()`) is a v1+ concern when retries / scheduled refresh / multi-tab usage become real.

**Sources for error classification:**
- [YouTube Data API — Errors | Google for Developers](https://developers.google.com/youtube/v3/docs/errors)
- [Global domain errors | YouTube Data API](https://developers.google.com/youtube/v3/docs/core_errors)
- [OAuth 2.0 for Mobile & Desktop Apps — refresh token expiration](https://developers.google.com/youtube/v3/guides/auth/installed-apps)

## Data retention (v0 scope, 2026-04-15)

| Data                                                 | Persistence                           | Notes                                                         |
| ---------------------------------------------------- | ------------------------------------- | ------------------------------------------------------------- |
| OAuth tokens (access + refresh)                      | `token.json` on disk, gitignored path | No app-level encryption; see note below                       |
| Raw API JSON (subs, likes lists)                     | **Not persisted**                     | Fetched → extracted → discarded in-process                    |
| `AgentMemory` (`profile_state` + `topic_multiplier`) | Persisted indefinitely                | Derivative of raw data; only persisted artifact of user taste |
| Topic-tag cache (`channel_id`/`video_id` → tags)     | Cross-user, persistent                | Public YouTube metadata, not PII                              |
| Alice's Day-0 JSON                                 | Committed to repo                     | One consented subject; see Auth model                         |

**No app-level token encryption in v0.** Threat model is small: `youtube.readonly` is a read-only non-sensitive scope; tokens sit on the dev machine behind FileVault (or equivalent disk encryption); `.gitignore` prevents commit; no hosted DB or non-dev users exist yet. Adding Fernet-style symmetric encryption here would be throwaway work — the v1 solution (KMS-backed, for a real hosted DB with real users) wouldn't reuse it. Revisit encryption posture when v1 introduces hosted storage.

**User-delete endpoint** is **not built for v0** — demo-scale, known users. For v1 public scope: cascade-delete tokens + `AgentMemory` on request; tag cache survives (not PII). See §V1+ open questions.

## V1+ open questions (parked)

- **DPAPI as a richer EU-only path.** When a user's Google account is in the EU/UK/CH, DPAPI becomes accessible and offers watch history (via `myactivity.youtube` scope). Defer. The `InterestProfile` schema is acquisition-agnostic — a DPAPI-backed adapter can populate it with richer inputs when the time comes, without breaking contracts.
- **Manual Takeout as a power-user tier.** Surface "want richer personalization? Upload your Google Takeout" in settings after first episode. Non-blocking. Feeds the same `InterestProfile` schema via a Takeout-parsing adapter that we're not building now.
- **Browser extension.** Scrapes YouTube history page via DOM for users who want real watch-history without Takeout or DPAPI. High engineering cost, high onboarding friction — parked unless the learning loop proves insufficient.
- **Two-level taxonomy.** Pair `topicCategories` (Level 1 canonical) with LLM sub-tags (Level 2 granular) for richer pitch-time reasoning. Deferred — probe shows topicCategories alone may be enough.
- **Cross-user topicDetails cache.** Server shares channel → tags map across all users. Low priority — cache-hit ratio grows naturally without engineering.
- **Explicit onboarding taste elicitation.** "Pick 3–5 channels you've been especially into lately" question at first-run to boost cold-start recency signal. Additive, non-blocking, skippable.
- **User-delete endpoint.** Cascade-delete OAuth tokens + `AgentMemory` on user request; topic-tag cache survives (cross-user public metadata, not PII). Required for v1 public launch. Out of scope for v0 demo.
- **Social-graph features ("see what a friend is into") need a different acquisition path.** The `LL` (liked videos) playlist via `playlistItems.list` is accessible only for the authenticated user's own account, not for arbitrary users. v1+ social features cannot reuse the v0 acquisition layer for non-self accounts; the extractor stays portable, but a social-graph acquisition adapter (likely public-channel-only, no likes) would need separate design.

## Dependencies on other components

| Component           | Contract                                                                                                                                      | Direction      |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| `agents` (parent)   | `DataAgent` protocol, `Pitch` shape, `Brief` shape                                                                                            | in             |
| `agents/alices`   | Imports shared extractor — `extract_profile(subs, likes, channel_topics, video_topics, now) -> InterestProfile`                               | out            |
| `learning-loop`     | `AgentMemory` shape, `EpisodeSignals` shape (both locked 2026-04-15, §`AgentMemory` schema); learning-loop owns `topic_multiplier` writes. **v0 stubbed** — no writes in v0; `topic_multiplier` stays `{}` unless fixture-seeded via `learning_loop.seed_topic_multiplier()` | in             |
| `api-storage`       | Persists `AgentMemory` via `agent_memory` table; caches OAuth tokens + topic-tag lookups (`channel_id` / `video_id` → tags)                   | in/out         |
| `producer`          | Consumes `list[Pitch]`                                                                                                                        | out            |
| YouTube Data API v3 | `subscriptions.list`, `playlistItems.list` (user OAuth); `channels.list?part=topicDetails` + `videos.list?part=topicDetails` (server API key) | external (out) |

## Build plan touchpoints (v0, 6-day window)

- **Day 0 (pre-build, completed 2026-04-14):**
  - ✅ Google Cloud project provisioned; YouTube Data API v3 enabled.
  - ✅ OAuth consent screen in testing mode with `youtube.readonly` scope; dev added as test user.
  - ✅ Desktop OAuth client created; `credentials.json` saved to `tmp/DPAPI/credentials.json` (path inherited from earlier DPAPI experiment). **Day 1 cleanup (2026-04-15):** *copy* (do not move/delete the original — old probe scripts may still reference it) the file to a properly-named path like `agents/youtube/secrets/credentials.json` (gitignored) and update new code to read from there. Old path remains as a backwards-compatible shim until all callers migrated.
  - ✅ YouTube Data API probe (`tmp/ydata/youtube_api_probe.py`) run against dev's account. Results saved to `tmp/ydata/probe_1776208130/`. Validated: subs, likes, topicCategories coverage, likes∩subs divergence.
  - **TODO Day 0 — Alice:** run the same probe against Alice's Google account (same OAuth testing-mode client, add Alice as test user). Save responses in the repo as `agents/alices/data/` for Day-4 consumption.
  - Provision dedicated server API key for `topicDetails` lookups (separate from user OAuth).
- **Day 1 — scaffold + lock extractor contract.**
  - Move the probe script into the repo proper (`scripts/youtube_api_probe.py` or `agents/youtube/scripts/probe.py`). Commit Day-0 JSON as a regression fixture for extractor testing.
  - Scaffold `DataAgent` protocol for `youtube_agent`; `fetch_context(user_id)` returns a stub `InterestProfile` built from the committed probe JSON; `pitch()` emits placeholder `Pitch` from first entity.
  - **Template hook stub:** implement `pitch()` with deterministic template hooks (e.g., "You've been subscribed to {channel} since {date}") so the pipeline works end-to-end without an LLM call. Day 3 replaces templates with LLM hooks. Templates remain as fallback if Day 3's LLM integration slips.
  - Integrates with calendar + weather agents to prove protocol works end-to-end via CLI.
- **Day 2 — extraction pipeline (shared with `alices_agent`).**
  - Build `agents/youtube/extractor.py` as a pure function: `extract_profile(subs, likes, channel_topics, video_topics, now) -> InterestProfile`. No I/O, no OAuth, no API calls.
  - Consume committed probe JSON as input. Produce topic-scored `InterestProfile` with both L1-normalized windows + K=5 compressed provenance.
  - Topic tagging acquisition: batch `channels.list?part=topicDetails` for subscribed channels + `videos.list?part=topicDetails` for liked videos. Cache both server-side.
  - Per-video topicDetails coverage already validated pre-Day-2 (89.6% on dev's 77 liked videos — see §Topic tagging). No LLM fallback needed.
  - Normalize Wikipedia URLs → kebab topic tags (strip parenthetical suffixes, `_` → `-`, lowercase).
  - Apply recency decay to like weights (90-day half-life) before feeding into `tf_recent`.
  - Compute TF-IDF per window: sublinear TF `log(1 + tf)`, per-window IDF `log((N_window + 1) / df_window)`, L1-normalize each non-empty window.
  - Build `topic_provenance` with K=5 per-topic cap (up to 2 oldest subs + up to 3 newest likes, fill continuing in same sort order on the over-supplied side if one side is short).
  - Unit tests lock the extractor contract — future acquisition changes can't drift the profile shape. Fixtures drawn from committed probe JSON.
- **Day 3 — `pitch()` generation.** Real `pitch()` logic using both profile windows + `AgentMemory`. Priority formula per `agents/docs` Reviewer Concern #1. Replace Day 1's template hooks with LLM-generated hooks constrained by `claim_kind` + `provenance_shape` (see `agents/docs/prompt_design.md` §1–§2). Unit tests for `compute_claim_kind()`, `compute_provenance_shape()`, and `select_segments()` must pass before the LLM prompt step is built (see prompt_design.md §Test mandate).
- **Day 4 — `alices_agent`.** Reuse `extract_profile()` on Alice's Day-0 JSON (one-time). Layer persona + content pack + wallet on top. This is the validation that the shared-extractor contract is clean.
- **Day 5 — learning-loop wiring (REQUIRED for Episode B demo beat, 2026-04-15).** Learning-loop consumes `EpisodeSignals` at session-end and writes `topic_multiplier` updates per its own rules (§Memory boundary). The demo plays **two episodes**: cold-start (Episode A) and post-learning (Episode B). The Episode-B-shifts beat is the demo's proof point that the learning loop is real, so this day is **non-negotiable, not stretch**. If learning-loop slips, Episode B looks identical to Episode A and the demo silently loses its core narrative.

## Success criteria

- `InterestProfile` builds from live YouTube Data API calls (dev's account) with both topic-score windows populated and `topic_provenance` non-empty for each scored topic.
- Per-channel `topicCategories` coverage ≥ 70% across all 96 subs (probe showed 100% on first 20 — should hold).
- Per-video `topicDetails` coverage ≥ 70% across dev's 77 liked videos — validated 2026-04-15 at 89.6% (69/77). No LLM fallback required.
- **TF-IDF shape is sensible:** broad tags (`lifestyle`, `entertainment`) rank lower than genre-specific tags despite higher raw frequency. Spot-check on dev's data: any `lifestyle`-heavy channel's genre-specific co-tags rank above `lifestyle` in the topic dict.
- **Temporal comparison is visible:** for at least one topic where recent likes concentrate (e.g., a genre the user has been exploring lately), `recent_topic_scores[T] > long_term_topic_scores[T]`. Inverse holds for a topic with many old subs but no recent likes.
- **Provenance is LLM-ready:** for any topic `T` with score > 0, `topic_provenance[T]` has 1–5 contributors with fully-populated fields (all required fields present, `kind`-discriminated optional fields correctly set).
- `pitch()` emits 3–5 valid `Pitch` objects on real data (no mocks) with priority ∈ [0, 1], **or** exactly 1 thin-signal `Pitch` when `combined_topic_scores == {}` per the §`pitch()` flow output contract.
- Profile fits the `DataAgent` protocol — no agent-specific escape hatches into Producer.
- `alices_agent` successfully constructs its profile by calling the shared extractor on committed Day-0 JSON.

**Spec status:** PARTIAL. Design decisions through 2026-04-15 are locked in the body above; v1+ items remain in §V1+ open questions. No v0-blocking questions remain. Next: execute build plan starting at Day 1 (scaffold + lock extractor contract).

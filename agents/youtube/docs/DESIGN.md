# Agent: `youtube_agent`

**Status:** DRAFT — PARTIAL SPEC (in-progress; decisions through 2026-04-14 brainstorming + empirical probe)
**Parent component doc:** [`../../docs/DESIGN.md`](../../docs/DESIGN.md) — `agents` component (shared `DataAgent` protocol, memory shape, `Pitch` shape)
**Master doc:** [`~/.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md`](../../../../.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md)
**Scope:** demo (v0) + v1 onboarding skeleton. Per brainstorming decision — v0 is v1's simplified implementation, not a disposable shortcut.
**Covers:** both the **internal `youtube_agent`** (user-selected) and the **shared YouTube interest-extraction pipeline** reused by `alices_agent` (external). Alice's agent is also a YouTube-seeded agent; duplicating the extraction pipeline would be an architectural smell.

## Purpose

Pitch 3–5 ranked topics to the Producer derived from the user's YouTube world. The agent owns:

1. **Interest profile extraction** — ingest the user's YouTube signals (subscriptions + liked videos) via YouTube Data API v3, produce a recency-aware `InterestProfile` that reflects both long-term taste and recent consumption.
2. **Pitch generation** — read the `InterestProfile` + `AgentMemory` + `Brief`, emit ranked `Pitch` objects.
3. **Memory update** — `observe()` updates per-user memory from `/react` signals (delegated to `learning-loop` rules).

This is the most design-heavy of the four agents: calendar and weather are structured-input → pitch; `alices_agent` is hand-curated content. YouTube is the only one where "what does a good interest profile look like?" is itself an open question.

## Two-layer architecture (decided)

Two separate data structures feed every `pitch()` call:

- **`InterestProfile`** — derived from the user's YouTube data. Lives inside `ScopeContext` (per `DataAgent` protocol). Recomputed by `fetch_context()` on every generation (inexpensive; mostly cache hits). Represents "what does this user care about on YouTube, including recency."
- **`AgentMemory`** — defined in [`learning-loop/docs/DESIGN.md`](../../../learning-loop/docs/DESIGN.md). Updated by `observe()` from `/react` signals. Represents "how have in-app reactions updated our beliefs."

Both are read by `pitch()`. They are orthogonal: the profile reflects off-platform taste; memory reflects in-platform feedback.

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

| Endpoint                                                | Result                                               | Notes                                                                 |
| ------------------------------------------------------- | ---------------------------------------------------- | --------------------------------------------------------------------- |
| `subscriptions.list?mine=true&part=snippet,contentDetails` | **96 subs**, `snippet.publishedAt` = subscribe date (2016→2026) | Real temporal signal. One quota unit per page of 50.                  |
| `playlistItems.list?playlistId=LL&part=snippet,contentDetails` | **77 likes** across **72 unique channels**, `snippet.publishedAt` = added-to-playlist time (2017→2026) | **LL playlist is accessible for own account** (contradicts some 2024 forum chatter). Real like timestamps. |
| `playlistItems.list?playlistId=WL`                      | 0 items                                              | Watch Later dead since Sep 2016. Do not query.                        |
| `playlistItems.list?playlistId=HL`                      | 0 items                                              | Watch History dead since 2016. Do not query.                          |
| `activities.list?mine=true`                             | 60 items, only types `playlistItem` + `upload`. No `like` / `subscription` / `favorite` activities. | **Not useful for interest profiling.** Drop from plan.                |
| `channels.list?id=<20 subs>&part=topicDetails`          | **100% coverage**, Wikipedia URLs                    | No LLM fallback needed for topic tagging on subscribed channels.      |

**Critical empirical finding — likes ∩ subs divergence:**

- 96 subs, 72 unique liked-video channels, **only 18 overlap**
- **49 liked-video channels are NOT subscribed**
- Likes reveal "drive-by interest" in channels the user consumes without committing. If we profiled off subs alone, we'd miss ~68% of the user's liked-content channel surface.

Implication for profile construction: **liked-video channels are first-class profile entities**, alongside subscribed channels. The profile pulls entities from the union, not just subscriptions.

### Endpoints consumed

| Call                                                                                | Purpose                                                          | Quota / call |
| ----------------------------------------------------------------------------------- | ---------------------------------------------------------------- | ------------ |
| `channels.list?mine=true&part=contentDetails`                                       | Resolve current user's `relatedPlaylists.likes` ID (usually `LL`) | 1            |
| `subscriptions.list?mine=true&part=snippet,contentDetails` (paginated, 50/page)     | Subscriptions with subscribe dates                               | 1 per page   |
| `playlistItems.list?playlistId={likes_id}&part=snippet,contentDetails` (paginated)  | Liked videos with channel attribution + like timestamps          | 1 per page   |
| `channels.list?id=<batch of up to 50>&part=topicDetails,snippet` (server API key)   | `topicCategories` for entity → topic rollup                      | 1 per batch  |

**Typical cost per user:** ~5 calls (subs page + likes page + 2–3 topic batches) ≈ 5 quota units per profile refresh. Default daily quota is 10,000 units → ~2,000 refreshes/day per project. Not a bottleneck.

**Not queried in v0 (confirmed-dead or low-signal):**

- `playlistItems.list?playlistId=WL` — dead
- `playlistItems.list?playlistId=HL` — dead
- `activities.list?mine=true` — returns only uploads + playlist adds, no user-taste activity types
- Custom user-created playlists — possible v1 fast-follow if they carry taste signal

## Shared extractor: `youtube_agent` + `alices_agent`

Both agents are YouTube-interest-profile-based. They share a pure extraction function:

```
agents/youtube/extractor.py       # (subs_json, likes_json, topics_json) → InterestProfile
agents/youtube/agent.py            # internal user agent (calls YouTube API live, then extractor)
agents/alices/agent.py           # external creator agent (loads Alice's Day-0 JSON, then extractor)
```

**What's shared** — the `extract_profile()` function: pure, takes API-response-shaped dicts, returns `InterestProfile`. No side effects. No OAuth coupling. No agent coupling.

**What's not shared** — each agent owns its own `pitch()`, per-agent memory, persona/voice metadata, and (for `alices_agent`) wallet + content pack. Each agent owns its own acquisition:

- `youtube_agent` acquires via live YouTube Data API OAuth on the user's account.
- `alices_agent` acquires via a **one-time Day-0 probe** against Alice's own account, with the resulting JSON responses checked into the repo as his static profile input. Alice never re-authenticates — his profile is frozen at Day 0 (with manual refreshes if his taste evolves between demo runs).

**Implication for this spec:** extraction sections below apply to both instances. `pitch()` and memory sections are specific to the internal user agent; `alices_agent`'s specifics (wallet, content pack, persona) live in `agents/alices/docs/DESIGN.md` (TBD).

## `InterestProfile` schema (decided — shape B: two time windows)

```python
class InterestProfile(TypedDict):
    long_term_entity_scores: dict[str, float]   # subscribed channels, weighted by subscribe-date recency
    recent_entity_scores:    dict[str, float]   # liked-video channels, weighted by like-date recency (includes non-subscribed channels)
    long_term_topic_scores:  dict[str, float]   # rolled up from long_term entities via topicCategories
    recent_topic_scores:     dict[str, float]   # rolled up from recent entities via topicCategories
    computed_at: str                             # ISO 8601; profile is recomputed per-generation
    stats: dict                                  # debug: total_subs, total_likes, tag_coverage_pct, subs∩likes overlap
```

**Entity sources — empirically grounded:**

- `long_term_entity_scores` keys = channels from `subscriptions.list` (96 for dev).
- `recent_entity_scores` keys = channels from the likes playlist (72 unique for dev) — **includes the 49 non-subscribed "drive-by" channels**. This is intentional: likes surface taste that subscriptions miss.
- Topic scores: union of all entity channels, fed to `topicDetails`, bucketed into long-term or recent based on which entity dict contributed them.

**Why two time windows (not a single flattened score):** "recency factored in" is meaningful only if `pitch()` can see both lenses separately. A single-number aggregate lets pitches say "the user likes X" but not "the user has been into X this week" vs "the user has been steadily subscribed to X for years." Those are different pitches. Rejected alternatives: flat-single-window loses the distinction; rich-per-entity-metadata is too much code for v0.

**Compatibility with `AgentMemory`:** both use `dict[str, float]` score maps keyed by channel-name or topic-tag. `pitch()` can merge or compare profile scores with memory scores using the same keys.

## Topic tagging (decided, validated 2026-04-14)

**Strategy:** server-side cache of `channel_id → topic_tags`, populated from YouTube Data API's `topicCategories` field. LLM fallback is documented but may be unnecessary — probe showed 100% coverage on real data.

**Why topicCategories works:**

- Returns a list of Wikipedia URLs describing the channel (e.g. `https://en.wikipedia.org/wiki/Electronic_music`). Canonical, human-readable, pre-normalized by Wikipedia. No aliasing across users.
- Public channel metadata → **server API key is sufficient**; no user OAuth required for the tagging step.
- `channels.list` accepts comma-separated channel IDs (up to 50), costs 1 quota unit per call. Default quota 10k units/day.
- **Empirical coverage: 100%** on the first 20 of dev's subscriptions (all 20 returned non-empty `topicCategories`).

**Normalization:**

```
https://en.wikipedia.org/wiki/Rock_music              → "rock-music"
https://en.wikipedia.org/wiki/Lifestyle_(sociology)    → "lifestyle"          # strip parenthetical
https://en.wikipedia.org/wiki/Video_game_culture       → "video-game-culture"
```

Rule: take last path segment, URL-decode, strip parenthetical suffixes `(...)` from the end, convert `_` → `-`, lowercase.

**LLM fallback (deferred to fast-follow):** if Day-1-scale validation across a larger channel set (user's full 96 subs + liked-video channels) shows meaningful broad-term pollution (`lifestyle`, `knowledge`, `entertainment` dominating and drowning granular signal), add a single-pass LLM enrichment: "given channel name + 3–5 recent video titles, output 1–3 kebab-case topic tags." Start without this — probe suggests it may not be needed.

**Graceful degradation:**

- Coverage drops → profile still works at coarse granularity.
- YouTube API over quota → cache absorbs it; fresh channels degrade to channel-name-based stub tags.
- Tag dict empty → `pitch()` must tolerate; falls back to entity-only reasoning.

## Auth model summary

| Auth                                     | Used by | Purpose                                                              |
| ---------------------------------------- | ------- | -------------------------------------------------------------------- |
| User OAuth `youtube.readonly`            | v0 + v1 | Fetch user's subs + liked videos via YouTube Data API v3             |
| Server API key (developer credential)    | v0 + v1 | `channels.list?part=topicDetails` for any channel (public metadata)  |

**One consent prompt, one scope, one API, globally available.** No regional gating, no async archive, no upload, no verification gate (`youtube.readonly` is a common non-sensitive scope — app verification is still required for >100 users in production but is straightforward).

**Alice's one-time profile:** same probe script run against Alice's account at Day 0, with the resulting JSON responses committed as his static input (not his OAuth token). No live OAuth for Alice's agent at runtime.

## Key decisions scoped through 2026-04-14 brainstorming

| #   | Decision             | Chosen                                                                                                                                | Alternatives rejected                                                                                                                               |
| --- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Design scope         | v0 demo = v1's simplified skeleton (Option B from brainstorm)                                                                         | v0 as disposable shortcut (C); v0 as demo-only (A)                                                                                                  |
| 2   | Acquisition layer    | **YouTube Data API v3, user OAuth `youtube.readonly`.** Single scope, synchronous, globally available.                                | DPAPI (US unavailable, async, verification gate); Manual Takeout upload (unacceptable consumer onboarding friction); Channel-handle paste (no recency) |
| 3   | Fields consumed      | `subscriptions.list` (+ subscribe dates), `playlistItems.list?playlistId=LL` (+ like dates), `channels.list?part=topicDetails`         | Activities API (empty of taste signals); Watch Later / Watch History (dead); Custom playlists (v1 fast-follow)                                      |
| 3b  | Shared extractor     | Pure function `extract_profile(subs_json, likes_json, topics_json) -> InterestProfile`, shared by `youtube_agent` and `alices_agent` | Duplicate per-agent extraction; per-agent ad-hoc logic                                                                                              |
| 3c  | Profile entity set   | Union of subscribed channels + liked-video channels. Likes add ~49 non-subscribed channels to the dev's profile surface.              | Subs-only (loses "drive-by interest"); Likes-only (loses baseline taste)                                                                            |
| 4   | Profile architecture | Two layers: `InterestProfile` (in `ScopeContext`) + `AgentMemory` (from signals)                                                      | Single unified memory; profile only                                                                                                                 |
| 5   | Profile shape        | Flat with two time windows (long-term + recent × entity + topic)                                                                      | Flat, single-window; rich per-entity metadata                                                                                                       |
| 6   | Topic tagging        | `topicCategories` only for v0 (100% coverage on probe). LLM fallback deferred to fast-follow.                                         | Pure LLM-per-channel; fixed taxonomy; skip topics; hand-tagged                                                                                      |
| 7   | Build order          | Design YouTube first (highest risk); build skeleton-first per master plan                                                             | Build YouTube first                                                                                                                                 |

## Open design questions (carry into next brainstorm session)

1. **Recency decay for `recent_entity_scores`.** Options: piecewise bucketed (1.0/0.5/0.25 at 30/60/90 days), exponential decay (half-life parameter), hard cutoff. Dev's oldest like = 2017-09, newest = 2026-04-13 — 8+ year range → decay shape matters a lot.
2. **"Recent" window length.** 30 / 60 / 90 / 180 days? Dev has 77 total likes across 8 years → only a handful per month on average. Shorter windows risk empty `recent` dicts; longer windows blur the signal.
3. **Long-term score assignment.** All subscriptions = 1.0 flat? Or weighted by subscribe-date recency (fresh subs > years-old subs)? Or decayed so a channel subbed 2016 counts less than one subbed 2026? Dev's oldest sub = 2016-02, newest = 2026-04-11 — decay is meaningful.
4. **Like aggregation per channel.** When a channel has 4 likes (dev's top: "大佬甜er" with 4), do we sum recency-decayed like weights, take max, or average? Likes are sparse — most channels have 1 like in the dataset.
5. **Subs ∩ likes overlap treatment.** A channel that's both subscribed AND has liked videos — is its score the sum of both windows, or is it in one window only? Proposal: long-term score from sub-date, recent score from sum of like-dates, treated as two independent entries keyed by the same channel name.
6. **`pitch()` signature.** How does `pitch()` consume both long-term + recent dicts? How does profile interact with `AgentMemory.entity_scores` at pitch-time (sum? max? weighted blend?)?
7. **Brief-driven filtering.** If the Brief says "morning briefing," do we filter the profile to a subset (news/tech) vs. Friday evening (movies/music)? Or is this always the Producer's job?
8. **Profile cache invalidation.** `computed_at` is in schema — invalidate on every `fetch_context()`? Cache for N minutes? Refresh on explicit "update my taste" action?
9. **Topic tag normalization on broad terms.** `Lifestyle_(sociology)` appeared on 7 of the first 20 subs — often as the _only_ tag (bad) or as one of many (acceptable). Do we drop topics that appear on >X% of a user's channels (noise filter), or keep all?
10. **Quota exhaustion path.** Default daily quota is 10k units. Under worst-case per-user usage (subs + likes pagination + topic lookups) we'd burn maybe 10–20 units per user. What's the path when quota is exceeded — serve stale cached profile, or degrade to memory-only pitching?
11. **V1 verification submission.** `youtube.readonly` is a standard scope but Google app verification is required for >100 users in production. Timeline and artifacts (privacy policy, scope justification, demo video) — budget how?

## V1+ open questions (parked)

- **DPAPI as a richer EU-only path.** When a user's Google account is in the EU/UK/CH, DPAPI becomes accessible and offers watch history (via `myactivity.youtube` scope). Defer. The `InterestProfile` schema is acquisition-agnostic — a DPAPI-backed adapter can populate it with richer inputs when the time comes, without breaking contracts.
- **Manual Takeout as a power-user tier.** Surface "want richer personalization? Upload your Google Takeout" in settings after first episode. Non-blocking. Feeds the same `InterestProfile` schema via a Takeout-parsing adapter that we're not building now.
- **Browser extension.** Scrapes YouTube history page via DOM for users who want real watch-history without Takeout or DPAPI. High engineering cost, high onboarding friction — parked unless the learning loop proves insufficient.
- **Two-level taxonomy.** Pair `topicCategories` (Level 1 canonical) with LLM sub-tags (Level 2 granular) for richer pitch-time reasoning. Deferred — probe shows topicCategories alone may be enough.
- **Cross-user topicDetails cache.** Server shares channel → tags map across all users. Low priority — cache-hit ratio grows naturally without engineering.
- **Explicit onboarding taste elicitation.** "Pick 3–5 channels you've been especially into lately" question at first-run to boost cold-start recency signal. Additive, non-blocking, skippable.

## Dependencies on other components

| Component             | Contract                                                                          | Direction      |
| --------------------- | --------------------------------------------------------------------------------- | -------------- |
| `agents` (parent)     | `DataAgent` protocol, `Pitch` shape, `Brief` shape                                | in             |
| `agents/alices`     | Imports shared extractor — `extract_profile(subs, likes, topics) -> InterestProfile` | out            |
| `learning-loop`       | `AgentMemory` shape, `EpisodeSignals` shape, `observe()` update rules             | in             |
| `api-storage`         | Persists `AgentMemory` via `agent_memory` table; caches OAuth tokens + profile blobs | in/out       |
| `producer`            | Consumes `list[Pitch]`                                                            | out            |
| YouTube Data API v3   | `subscriptions.list`, `playlistItems.list` (user OAuth); `channels.list?part=topicDetails` (server API key) | external (out) |

## Build plan touchpoints (v0, 6-day window)

- **Day 0 (pre-build, completed 2026-04-14):**
  - ✅ Google Cloud project provisioned; YouTube Data API v3 enabled.
  - ✅ OAuth consent screen in testing mode with `youtube.readonly` scope; dev added as test user.
  - ✅ Desktop OAuth client created; `credentials.json` saved to `tmp/DPAPI/credentials.json` (path inherited from earlier DPAPI experiment; rename later).
  - ✅ YouTube Data API probe (`tmp/ydata/youtube_api_probe.py`) run against dev's account. Results saved to `tmp/ydata/probe_1776208130/`. Validated: subs, likes, topicCategories coverage, likes∩subs divergence.
  - **TODO Day 0 — Alice:** run the same probe against Alice's Google account (same OAuth testing-mode client, add Alice as test user). Save responses in the repo as `agents/alices/data/` for Day-4 consumption.
  - Provision dedicated server API key for `topicDetails` lookups (separate from user OAuth).
- **Day 1 — scaffold + lock extractor contract.**
  - Move the probe script into the repo proper (`scripts/youtube_api_probe.py` or `agents/youtube/scripts/probe.py`). Commit Day-0 JSON as a regression fixture for extractor testing.
  - Scaffold `DataAgent` protocol for `youtube_agent`; `fetch_context()` returns a stub `InterestProfile` built from the committed probe JSON; `pitch()` emits placeholder `Pitch` from first entity.
  - Integrates with calendar + weather agents to prove protocol works end-to-end via CLI.
- **Day 2 — extraction pipeline (shared with `alices_agent`).**
  - Build `agents/youtube/extractor.py` as a pure function: `extract_profile(subs_json, likes_json, topics_json) -> InterestProfile`. No I/O, no OAuth, no API calls.
  - Consume committed probe JSON as input. Produce `InterestProfile` with both windows populated.
  - Apply recency decay → `recent_entity_scores` (formula per open question #1).
  - Batch `channels.list?part=topicDetails` calls across all entity channels (subs + liked-video channels); cache responses.
  - Normalize Wikipedia URLs → kebab topic tags; roll up to topic_scores per window.
  - Unit tests lock the extractor contract — future acquisition changes can't drift the profile shape.
- **Day 3 — `pitch()` generation.** Real `pitch()` logic using both profile windows + `AgentMemory`. Priority formula per `agents/docs` Reviewer Concern #1.
- **Day 4 — `alices_agent`.** Reuse `extract_profile()` on Alice's Day-0 JSON (one-time). Layer persona + content pack + wallet on top. This is the validation that the shared-extractor contract is clean.
- **Day 5 (stretch, Approach B):** `observe()` wires `/react` signals into memory updates per `learning-loop` rules.

## Success criteria

- `InterestProfile` builds from live YouTube Data API calls (dev's account) with non-empty both windows and non-empty topic dicts.
- `topicCategories` tag coverage ≥ 70% across the full 96-sub + liked-video channel set (probe showed 100% on first 20 subs — should hold).
- `pitch()` emits 3–5 valid `Pitch` objects on real data (no mocks), priority ∈ [0, 1].
- Recency signal is _visible_: a channel liked this month ranks higher in `recent_entity_scores` than a channel liked in 2018.
- Profile fits the `DataAgent` protocol — no agent-specific escape hatches into Producer.
- `alices_agent` successfully constructs its profile by calling the shared extractor on committed Day-0 JSON.

## Reviewer concerns (specific to YouTube Data API path)

### 1. The thin-signal worry (severity: medium)

With no watch history, the profile leans on subs (long-term only) + likes (sparse — dev has 77 likes across 8 years). A user who never likes videos has an effectively empty `recent_entity_scores`. Mitigation paths:

1. **Learning loop dominance** — after Episode 1, in-app `/react` signals carry the recency weight via `AgentMemory`. The `InterestProfile` is the cold-start seed; `AgentMemory` is the running state. For returning users, thin profile is fine.
2. **Optional onboarding taste elicitation** — "any channels you've been especially into lately?" one-line input. Additive fast-follow; not v0 scope.
3. **Narrative reframing** — for the demo, a thinner cold-start makes the Episode B "memory shifts" beat _more_ dramatic, not less. Feature, not bug.

### 2. Zero-likes users (severity: low-medium)

A user who has never clicked like on a YouTube video will have empty `recent_entity_scores`. Profile falls back to subs-only, with recency driven by subscribe-date only. Documented fallback; `pitch()` must tolerate empty recent dicts.

### 3. Own-account `LL` playlist access is special (severity: low)

The `LL` (liked videos) playlist is accessible via `playlistItems.list` for the authenticated user's own account but not for arbitrary users. Future features like "see what a friend is into" won't work through this endpoint. Flagged for v1+ social-graph designs.

---

**Spec status:** PARTIAL. Decisions 1–7 locked (decisions 2 + 3 refactored 2026-04-14 after empirical probe; 3b + 3c added). Open questions 1–11 need resolution before implementation plan. Next brainstorming session: recency decay + `pitch()` signature.

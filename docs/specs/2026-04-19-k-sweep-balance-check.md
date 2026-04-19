# K-sweep balance check for pre-LLM pitch pool

**Status:** APPROVED — brainstorming cleared 2026-04-19
**Parent docs:**

- Agents extractor: [`agents/youtube/extractor.py`](../../agents/youtube/extractor.py) — `BLEND_HALF_SATURATION_K`, `extract_profile()`
- Agents spec: [`agents/youtube/docs/DESIGN.md`](../../agents/youtube/docs/DESIGN.md) — long-term/recent windows, blend
- YouTube agent: [`agents/youtube/agent.py`](../../agents/youtube/agent.py) — candidate assembly, `_top_n_seeded`
- Alices agent: [`agents/alices/agent.py`](../../agents/alices/agent.py) — same extractor, pre-captured data

**Scope:** diagnostic script. Not a behavior change, not a test, not a production code change.

## Purpose

Before any LLM runs, each agent builds a deterministic top-8 candidate pool from the blended long-term (subs) and recent (likes) TF-IDF windows. The observed concentration of final Producer selections toward music topics is suspected to originate in this pool: the user's liked-videos window skews heavily music, and the current blend parameter `BLEND_HALF_SATURATION_K = 5.0` makes the recent window dominate the combined scores on both agents' datasets.

This spec defines a one-off sweep harness that answers a single question:

> Is there a value of K in `[0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500]` at which **both** agents' top-8 pre-LLM candidate pools contain ≤3 music topics on the committed probe data?

The answer informs whether retuning K is sufficient to balance the input pool, or whether the likes-concentration is structural (in which case a different intervention — explicit sub/like weights, a per-category cap during candidate selection, or taxonomy-aware diversification — would be needed).

## Non-goals

- **Not a production code change.** `extractor.py` and both agents remain untouched.
- **Not a committed test.** No regression assertion. Probe data is dev-only; once live OAuth replaces it, a pinned K assertion would become dead weight.
- **Not end-to-end proof.** A balanced pool does not guarantee a balanced Producer script — the Producer LLM still selects from the pool. The sweep only measures the input to the agent's LLM hook step, which is the same input the Producer ultimately pulls from.
- **No sweep over `RECENT_HALF_LIFE_DAYS`, memory multipliers, or per-category caps.** Deferred.

## Architecture

```
scripts/sweep_k.py
  |
  +-- For each agent in {youtube, alices}:
  |     |
  |     +-- Load committed probe JSON (inline loader, mirrors agent loaders).
  |     +-- Call extract_profile(subs, likes, channel_topics, video_topics, now).
  |         Capture: long_term_topic_scores, recent_topic_scores, stats.total_recent_weight.
  |         This call happens ONCE per agent — K does not affect either window.
  |
  +-- For each K in [0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500]:
        |
        +-- For each agent:
              |
              +-- alpha = total_recent_weight / (total_recent_weight + K)
              +-- combined[t] = (1-alpha)*long_term[t] + alpha*recent[t]  for t in union(keys)
              +-- combined = L1-normalize(combined)
              +-- candidates = _top_n_seeded(combined, n=8, seed=("sweep", computed_at))
              +-- music_count = sum(is_music(t) for t in candidates)
              +-- Print table row: K, alpha, music_count, candidates (music starred)
        |
        +-- Track per-agent music_count per K; after the sweep loop, print:
              "Smallest K with music_count <= 3 for both agents: K=<value>"
              or "none — likes-concentration is structural at this scope".
```

### Re-blend externally, not inside `extract_profile`

`extract_profile()` already returns the two windows and `total_recent_weight` separately. The script re-does only the blend step — the expensive work (parsing, TF-IDF, decay, provenance) runs once per agent.

This also means no `k` parameter gets added to `extract_profile` for a diagnostic. Production stays untouched.

### Seed handling

`_top_n_seeded` uses `(user_id, profile["computed_at"])` as its tiebreak seed. The script pins `user_id="sweep"` and reuses the `computed_at` from the single extract call per agent, so tie-breaks are consistent across all K rows for that agent — only the score re-blending changes what enters the top-8.

### Memory multiplier

In v0 agent memory is bootstrapped to `topic_multiplier = {}`, making `multiplier.get(t, 1.0)` the identity function ([agents/youtube/agent.py:235-239](../../agents/youtube/agent.py#L235-L239)). The sweep skips the multiplier step entirely — it would be a no-op on v0 data.

## Music rule

```python
def is_music(topic: str) -> bool:
    return (
        topic == "music"
        or topic.endswith("-music")
        or topic.startswith("music-of-")
        or topic in {"jazz", "rhythm-and-blues"}
    )
```

Covers the tags observed in the committed probe data:

- Exact: `music`
- `*-music` suffix: `pop-music`, `rock-music`, `classical-music`, `electronic-music`, `hip-hop-music`, `soul-music`, `independent-music`, `christian-music`
- `music-of-*` prefix: `music-of-asia`, `music-of-latin-america`
- Explicit: `jazz`, `rhythm-and-blues`

Explicitly excluded: `music-video-game` (classified as gaming, since the pitch would be about rhythm games, not music).

## Output format

Markdown-ish table printed to stdout, grouped by K:

```
K=0.5
  youtube   α=0.94   music/8=5   [*music, *pop-music, lifestyle, technology, *jazz, entertainment, *rock-music, knowledge]
  alices  α=0.98   music/8=4   [*music, lifestyle, video-game-culture, *rock-music, technology, *jazz, *pop-music, knowledge]

K=1
  ...

[...]

Summary: smallest K with music/8 ≤ 3 for BOTH agents: K=50
(Or: "none in sweep range — likes-concentration is structural.")
```

Asterisks mark music topics. Alpha per row makes it visible why the two agents behave differently at the same K (different `total_recent_weight`).

## What this won't catch

- **Producer-side selection bias.** The Producer LLM picks from the union of agents' pitches. Even with a balanced pre-LLM pool, the Producer could still over-select music-framed pitches. This is diagnosable only downstream.
- **Topic taxonomy gaps.** If YouTube's `topicCategories` mis-tags a music channel as `entertainment`, the sweep won't notice — it can only see the tags the API provided.
- **Within-genre diversity.** A top-8 of `[pop-music, rock-music, jazz, electronic-music, ...]` counts as 4/8 music. The sweep treats all music subgenres as one bucket by design (that's the balance goal the user requested).

## Deliverable

Single file: `scripts/sweep_k.py`. Standalone, runnable via `python3 scripts/sweep_k.py` from repo root. No pytest integration, no new dependencies, no modifications to existing code.

## Acceptance

- Running the script from repo root produces the table described in Output format.
- The sweep includes all 10 K values against both agents.
- Summary line correctly identifies the smallest K meeting the ≤3/8 criterion on both agents, or reports that none do.
- `extractor.py` and both agents are unchanged.

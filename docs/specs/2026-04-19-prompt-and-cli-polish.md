# Prompt and CLI Polish — 2026-04-19

**Status:** SHIPPED
**Scope:** Producer script prompts, YouTube/Alices pitch prompts,
CalendarAgent hook, TodayContext shape, CLI live output, per-episode
artifact storage.
**Driver:** post-run audits of [output.txt](../../output.txt) …
[output4.txt](../../output4.txt) revealed factless segments, calendar
hallucinations, citation tags in audio, abrupt sign-offs, and verbose CLI
output that didn't capture the full audit trail alongside the MP3s.

This spec is the single source of truth for everything shipped in the
2026-04-19 session. Domain DESIGN.md files have been patched where
specific details drift from code; this file carries the rationale.

## Contents

- [P2 — Widen segment framing beyond news](#p2--widen-segment-framing-beyond-news)
- [P3 — Preserve Chinese / Japanese script verbatim](#p3--preserve-chinese--japanese-script-verbatim)
- [P5 — Beat ratios rewritten for 80% factual body](#p5--beat-ratios-rewritten-for-80-factual-body)
- [P7 — Strip `<cite>` / `<br>` tags](#p7--strip-cite--br-tags)
- [P8 — Sign-off close-beat + 12s duration](#p8--sign-off-close-beat--12s-duration)
- [N1 — Calendar 16-hour window awareness](#n1--calendar-16-hour-window-awareness)
- [N2 — Alices takeaway focus rule](#n2--alices-takeaway-focus-rule)
- [N3 — Title-shape rule + generic-trend fallback](#n3--title-shape-rule--generic-trend-fallback)
- [P6 — CLI one-liner output + per-episode artifacts](#p6--cli-one-liner-output--per-episode-artifacts)
- [P1 — Topic diversity (dropped)](#p1--topic-diversity-dropped)

---

## P2 — Widen segment framing beyond news

**Problem.** The segment SYSTEM_PROMPT forced every taste segment to be "a
story or news item." Music topics with no active news angle drifted into
generic trend-piece content; historical / backstory / cultural facts were
off-limits by framing.

**Change.** [producer/script.py](../../producer/script.py) SYSTEM_PROMPT
§Research via web_search now instructs:

> Find a real-world angle on the topic — a current news item, a historical
> or backstory fact, a notable cultural moment, or a concrete detail about
> the people, places, or works involved. Pick whichever is most
> interesting today; don't force a news peg. Older or evergreen material
> is welcome when it's the strongest angle on the topic.

Renames: "Story lead" → "Lead" throughout. Drops the "append today's date
for freshness framing" guidance. Fallback language: "nothing fresh within
30 days" → "nothing usable as an angle."

---

## P3 — Preserve Chinese / Japanese script verbatim

**Problem.** Listener reads/hears English and Chinese fluently, recognises
some Japanese. Source_refs frequently contain titles in CJK scripts
(`有時寂寞`, `フォークダンス`). Prompt was silent, inviting translation /
pinyin / romaji.

**Audio verified.** [audio/config.py](../../audio/config.py) uses
`eleven_turbo_v2_5` which auto-detects 32 languages including CJK. No
TTS-side config change needed; leaving CJK in the script is sufficient.

**Change.** Added a `## Bilingual handling` block to all four producer
prompts in [producer/script.py](../../producer/script.py):
- `SYSTEM_PROMPT` (main segment prompt) — full block with
  "other non-English languages out of scope" clause
- `OPENER_SYSTEM_PROMPT`
- `SIGN_OFF_SYSTEM_PROMPT`
- `_HOOK_FALLBACK_SYSTEM_PROMPT` — inline paragraph

The rule: Chinese / Japanese proper nouns, titles, phrases, quotes stay in
their original script — no translation, pinyin/romaji, or parenthetical
glosses. English narration around them is fine.

---

## P5 — Beat ratios rewritten for 80% factual body

**Problem.** Old beats `Story lead ~20% / Development ~55% / Takeaway
~25%` left too much room for sweeping commentary and "what this means for
the listener" meandering. Sample: "Classics Meet New Anthems" segment
from [output2.txt](../../output2.txt) had essentially zero facts.

**Change.** New beat block in SYSTEM_PROMPT:

- **Lead** (~10%) — drop into the angle, 1–2 sentences.
- **Factual body** (~70%) — concrete facts from research: named
  people, works, places, dates, numbers, quotes, causes/effects. At
  least four distinct factual sentences. Commentary and emotional
  adjectives stay out of this band.
- **Flex band** (~10%) — one short passage for flow. Either another fact,
  a vivid detail, or a single line of commentary / emotional framing.
  Model's judgment on which the segment needs.
- **Takeaway** (~10%) — one sentence that lands the segment.

Test `tests/test_script.py::test_has_narration_contract_block` updated to
check for the new beat names.

---

## P7 — Strip `<cite>` / `<br>` tags

**Problem.** Anthropic `web_search` responses inject `<cite index="…">…
</cite>` citation markers into the model's text. Some responses also
emit `<br>` for line breaks. Both leak into `seg["script"]` and become
garbage audio at TTS time.

**Change — defense in depth.**

1. **Post-process (belt).** New `_strip_inline_markup(text)` helper in
   [producer/script.py](../../producer/script.py). Regex-strips
   `<cite …>content</cite>` preserving inner content; replaces
   `<br>` / `<br/>` with space; collapses double spaces. Applied on
   every generation path:
   - `_read_cached_segment` — cleans stale cache files on load
   - `generate_segment` — strips script + segue_in before validation
   - `generate_opener` — strips before length check
   - `generate_sign_off` — strips before return
2. **Prompt (suspenders).** Added "no inline markup" instruction to all
   four producer prompts.

**Tests.** 6 new `TestStripInlineMarkup` cases in
[tests/test_script.py](../../tests/test_script.py) covering single-cite,
multi-cite, `<br>` variants, cross-line cite, plain-text passthrough, and
double-space collapse.

---

## P8 — Sign-off close-beat + 12s duration

**Problem.** Sign-off was too abrupt — "Clear skies and cold out there
tonight — bundle up, stay warm, and have a good one." Listener had no
signal that the personalized feed for today was done.

**Change.**

- `SIGN_OFF_SYSTEM_PROMPT` rewritten with two internal beats:
  - **Close beat (~70%)** — signals "today's feed / picks for today is
    over", with varied phrasing. Optional soft tie to weather/calendar if
    it lands cleanly.
  - **Parting line (~30%)** — short, warm close. No "see you next time
    on [show name]" branding; the show is unnamed.
- Vocabulary rule: "podcast" and "episode" are banned as on-air words.
  Use "today's feed" / "today's show" / "today's picks" instead.
- `_SIGN_OFF_DURATION_SEC` 10 → 12 (`target_words` auto-scales to ~26w
  at 130 wpm).

---

## N1 — Calendar 16-hour window awareness

**Problem.** [agents/calendar/agent.py](../../agents/calendar/agent.py)
fetches only the next 16 hours, but the OPENER_SYSTEM_PROMPT didn't tell
the LLM that. Result: opener said "Calendar-wise you've got just the one
thing … the morning and evening are wide open" when the listener actually
had two more events later that day that were outside the window.

**Change.**

1. **`TodayContext` gains a `now` field**
   ([agents/protocol.py](../../agents/protocol.py)) — 24-hour local time
   `HH:MM:SS`. Date is already carried by the `date` field, so `now`
   stays compact. `TodayContext` is now `total=False` so existing tests
   that build partial dicts don't break.
2. **Orchestrator populates it**
   ([agents/orchestrator.py](../../agents/orchestrator.py)) with
   `now.strftime("%H:%M:%S")`.
3. **Calendar hook echoes the window.** The WHAT line now reads:
   > Schedule preview — N event(s) in the rolling 16h window from
   > {now_iso} to {now+16h_iso}. This is a horizon view, not the full
   > day.
4. **OPENER_SYSTEM_PROMPT gets a §Calendar window awareness block.** The
   LLM is told to reason from `today_context.now`: "if `now` is 08:00
   Monday, you have seen events through 00:00 Tuesday — morning and
   early afternoon are in view, later evening and tomorrow are NOT."
   Describe only what's in the window; say "nothing on the immediate
   horizon" rather than "open day" when empty.

Approach difference from original proposal: rather than blacklist
specific phrases ("wide open", "just the one thing"), we give the LLM
the structured facts (now + window length) and let it reason. Blacklists
don't generalise.

---

## N2 — Alices takeaway focus rule

**Problem.** Alices segment from [output2.txt](../../output2.txt) ended
with "For Alice's lens, this sits right at the intersection of
everything he's been gravitating toward lately — the rigor of the
Baroque tradition, played with real feeling by someone who clearly grew
up both with and beyond it. If the Goldbergs haven't crossed Alice's
radar yet, this is the version to start with." That's a portrait of
Alice, not a payoff for the listener.

**Change.** Added a **Takeaway focus rule** under §Per-agent provenance
semantics → `alices` in
[producer/script.py](../../producer/script.py) SYSTEM_PROMPT:

> The takeaway addresses the LISTENER — what's worth the listener's
> attention in this angle. Do NOT speculate about Alice's motivations,
> arc, identity, or what "fits Alice's lens / radar / taste tree".
> Alice's name appears once in the takeaway at most, as curator
> attribution only. The segment is the listener's daily feed; Alice is
> a sourced voice inside it, not the subject.

P5's ratio change to ~10% takeaway also structurally starves this
failure mode — there's no longer budget for Alice-portrait prose.

---

## N3 — Title-shape rule + generic-trend fallback

**Problem.** Factless segments ("Classics Meet New Anthems", "Film Fever
Taking Over") traced back upstream to poetic pitch titles. The producer
derives its web_search query from `title`; "classics meet new anthems"
returns think-piece fluff, "Bach violin repertoire" returns Yunchan
Lim / Goldberg Variations / Carnegie Hall.

**Upstream fix — title shape rule in pitch prompts.** Added as rule 6 in
both `YOUTUBE_SYSTEM_PROMPT` and `PATRICKS_SYSTEM_PROMPT` in
[agents/youtube/llm.py](../../agents/youtube/llm.py):

> Titles must include at least one concrete topical anchor — a genre,
> era, named public artist or work, decade span, or recognizable
> sub-movement. Titles drive a downstream web search; if a search engine
> would return generic think-pieces for the title, rewrite it. Titles
> are producer-internal handles, not on-air — favor searchability over
> radio-style flair.

Examples include "Bach violin repertoire" (good), "Alice's Classical
Picks" / "Classics Meet New Anthems" (bad — too generic).

**Producer safety net — generic-trend fallback.** Added to SYSTEM_PROMPT
§Research:

> If the search returns only sweeping trend-piece content — "audiences
> are embracing X", "several forces are aligning", "fans are creating",
> "what sets modern X apart" — with no named people, works, dates,
> places, or numbers, treat that as nothing usable and fall back
> (broadened retry, then hook-narration). A segment with real named
> facts from the hook beats a segment made of industry-think-piece
> vapor.

---

## P6 — CLI one-liner output + per-episode artifacts

**Problem.** The CLI dumped raw JSON of pitches / selected / episode
script to stdout at every step — hard to scan, harder to audit across
runs. The segment cache under `tmp/test_outputs/segment_scripts/` lived
separately from the MP3s, so a run's inputs, selections, and outputs
were scattered.

**Change — CLI one-liner output.** [agents/orchestrator.py](../../agents/orchestrator.py)
`cli_main` now prints an 8-step numbered summary:

```
[orchestrator] Episode {uuid} | user={user}

[setup]  Learning hydration   bootstrap (no prior signals)
[1/8] Brief assembled      date=YYYY-MM-DD Dow time, weather=…, calendar=N events
[2/8] Pitches collected    calendar=1  alices=5  weather=1  youtube=5
         External agent    invoked @GoddamnAxl ($0.10 USDC, tx=0x…)
[3/8] Producer memory      identity transform (bootstrap)
[4/8] Guaranteed slots     4 slots, 140s bonus budget
[5/8] Bonus selection      1 bonus picked → 5 segments, 345s total
[6/8] Running order        [calendar/30s]  [alices/90s]  …
[7/8] Script generation    opener Xw  seg1 Ys  seg2 Zs  …  sign-off Nw
[8/8] Audio export         data/episodes/{uuid}/ (N segments)
         Concat MP3         exports/episode-{uuid}.mp3

Artifacts: data/episodes/{uuid}/
```

Errors break out inline (`FAILED: …`). Playback and learning-preview
sections follow as before.

**Change — per-episode artifacts.** New module
[storage/episode_artifacts.py](../../storage/episode_artifacts.py) with
atomic JSON / text writers. The CLI saves everything alongside the
`segment_*.mp3` files in `data/episodes/{episode_id}/`:

| File | Source |
| --- | --- |
| `brief.json` | assembled `Brief` |
| `pitches.json` | raw pitches before memory |
| `pitches_post_memory.json` | pitches after `apply_producer_memory` |
| `guaranteed.json` | Phase-1 selection + budget |
| `bonus.json` | Step-1.5 bonus picks + reasoning |
| `running_order.json` | final ordered list with guaranteed/bonus tags |
| `opener_input.json` / `opener_output.txt` | LLM payload + text |
| `segment_{i}_input.json` / `segment_{i}_output.json` | per-segment |
| `sign_off_input.json` / `sign_off_output.txt` | LLM payload + text |
| `episode_script.json` | final assembled `EpisodeScript` |

**Supporting refactor — public payload builders.** Three new public
helpers in [producer/script.py](../../producer/script.py):
`build_opener_payload`, `build_segment_payload`, `build_sign_off_payload`.
Single source of truth for the LLM user-message JSON — used by both the
generators and the CLI artifact saver, so the on-disk record matches
what the LLM saw verbatim. `producer/tmp/test_integration.py`'s mirror
functions can be replaced with these in a follow-up.

**Supporting refactor — inlined script orchestration.** The CLI inlines
the opener/stream/sign_off calls (no longer routes through
`run_episode_pipeline`) so it can interleave artifact saves. `pipeline.py`
stays for non-CLI callers.

---

## P1 — Topic diversity (dropped)

The original proposal added `topic: str` to `Pitch` and used it to filter
repeated topics from the bonus-selection LLM payload. Deferred during the
session to revisit after the prompt changes landed; user chose to ditch
it. Rationale: N3's title-shape rule produces more topically distinct
titles organically, and the bonus LLM's existing claim_kind-diversity
heuristic gives decent spread. Revisit if real runs show persistent
topic clustering.

---

## Test impact

- Before: 456 passing, 19 failing (pre-existing test pollution around
  `DISABLE_LLM` env var).
- After: 463 passing, 19 failing (same pollution set).
- Net: +7 passing, 0 regressions.
- New tests: 6 × `TestStripInlineMarkup`, updated beat-name test.

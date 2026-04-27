# radio-podcast

AI-generated personal radio — a multi-agent system that produces a daily audio episode tailored to your weather, calendar, and YouTube subscriptions.

## Usage (v0 — CLI only)

v0 is a CLI. Select internal agents with flags:

```bash
python -m agents.orchestrator --weather --calendar --youtube
```

Absent flag → that agent is skipped. You must pass at least one.

**Auth prereqs — the CLI runs each the first time you activate its agent:**

- `--weather`: browser geolocation flow (artifact will be stored: `~/.config/radio-podcast/weather_location.json`)
- `--calendar`: Google OAuth flow (artifact will be stored: `~/.config/radio-podcast/calendar_token.json`); please contact Wanli to get your google account added to the app to use OAuthl
- `--youtube`: YouTube Data API OAuth + capture (requires `app_credential/credentials.json`; artifact will be stored: `ydata/user/02_subscriptions.json` or `$YOUTUBE_PROBE_DIR`); please contact Wanli to get your google account added to the app to use OAuth

**Playback hotkeys (during `afplay` playback):**

- `l` = like (1.10× on that agent's next-run weight)
- `s` = skip to next segment (0.90×)
- `r` = repeat current segment (1.20×)
- `p` / `space` = pause / resume (not a learning signal)
- `q` = quit episode

Feedback signals append to `./data/signals/{user_id}/{episode_id}.jsonl`. Next run hydrates `ProducerMemory` from the log; episode-over-episode re-ordering is visible via the end-of-run weight-delta preview.

**Other flags:**

- `--no-llm`: skip Producer LLM (segment JSON only; cheap smoke)
- `--no-external`: skip the external/Alices round + agentic-payment step
- `--no-export`: skip the ffmpeg concat at end of run (dev iteration)
- `--user-id <id>`: override default `dev` (isolates feedback signals per user)

**Remove credentials for re-auth:**

```
rm -f ~/.config/radio-podcast/calendar_token.json \
       ~/.config/radio-podcast/user_profile.json \
       ~/.config/radio-podcast/weather_location.json && \
rm -rf tmp/ydata/
```

**Remove prior memory (agent + producer layers):**

```
rm -rf data/agent_memory/ data/signals/
```

Agent memory lives at `data/agent_memory/{user_id}/{agent}.json` (v0 scaffold — reads return `{}` until learning-loop unstubs in v1). Producer memory is hydrated each run from `data/signals/{user_id}/*.jsonl`; deleting the signals log resets agent-weight re-ordering to the bootstrap identity.

**Tune topic weights (`config/topic_weights.toml`):**

Edit `config/topic_weights.toml` to boost or damp specific topics — or whole categories — per agent. The file is committed so tuning is visible in git history.

```toml
[categories]
music = ["rock-music", "pop-music", "jazz", "classical-music", ...]

[weights.youtube]
music = 0.5               # ×0.5 applied to every topic in the category
"action-game" = 0.7       # per-topic override (wins over category)

[weights.alices]
music = 0.3
```

- Values are clamped to `[0.1, 10.0]`. `1.0` is identity (no effect); `<1.0` damps; `>1.0` boosts.
- Precedence: per-topic entry > category entry > `1.0` default for any topic you don't name.
- Applied as a pure multiplication on each agent's `combined_topic_scores` **before** top-N candidate selection — damped topics become less likely to reach the LLM pitch bundle; boosted topics rise toward the top.
- Read once per CLI invocation at orchestrator startup (look for `[setup] Topic weights ...` in the run output); edits take effect on the next run.

**Output locations:**

- Per-segment TTS: `./data/episodes/{episode_id}/segment_{n}.mp3`
- Concat'd judge-handoff MP3: `./exports/episode-{episode_id}.mp3`
- Feedback signals: `./data/signals/{user_id}/{episode_id}.jsonl`

> **v1 scope:** Frontend selection UI + web player replace this CLI entirely. See `TODOS.md`.

## Architecture

- `agents/orchestrator.py` — top-level runner; flag-driven agent selection + auth preflight
- `agents/weather/`, `agents/calendar/`, `agents/youtube/`, `agents/alices/` — per-agent pitch modules
- `pipeline.py` — Producer LLM, segment selection, TTS, ffmpeg export
- `storage/` — per-agent storage helpers (episode dir, signals, agent memory, export)
- `auth/` — OAuth flows for calendar and YouTube
- `learning_loop/` — ProducerMemory hydration from feedback log

## Requirements

- Python 3.12+, macOS only (uses `afplay` for playback)
- `ffmpeg` on PATH (for MP3 concat export)
- ElevenLabs API key (`ELEVENLABS_API_KEY`)
- Anthropic API key (`ANTHROPIC_API_KEY`)
- YouTube Data API credentials (`app_credential/credentials.json`) for `--youtube` - please contact Wanli for access

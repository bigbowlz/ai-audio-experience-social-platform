# radio-podcast

AI-generated personal radio — a multi-agent system that produces a daily audio episode tailored to your weather, calendar, and YouTube subscriptions.

## Usage (v0 — CLI only)

v0 is a CLI. Select internal agents with flags:

```bash
python -m agents.orchestrator --weather --calendar --youtube
```

Absent flag → that agent is skipped. You must pass at least one.

**Auth prereqs — the CLI runs each the first time you activate its agent:**

- `--weather`: browser geolocation flow (artifact: `~/.config/radio-podcast/weather_location.json`)
- `--calendar`: Google OAuth flow (artifact: `~/.config/radio-podcast/calendar_token.json`)
- `--youtube`: YouTube Data API OAuth + capture (requires `tmp/DPAPI/credentials.json`; artifact: `tmp/ydata/probe_1776208130/02_subscriptions.json` or `$YOUTUBE_PROBE_DIR`)

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
- YouTube Data API credentials (`tmp/DPAPI/credentials.json`) for `--youtube`

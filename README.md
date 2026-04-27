# radio-podcast

AI-generated personal radio — a multi-agent system that produces a daily audio episode tailored to your weather, calendar, and YouTube subscriptions.

## Setup

### Prerequisites

- Python 3.12+, macOS only (uses `afplay` for playback)
- `ffmpeg` on PATH — install via `brew install ffmpeg`

### Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

### Environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in:

| Variable | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) → API Keys |
| `ELEVENLABS_API_KEY` | [elevenlabs.io](https://elevenlabs.io) → Profile → API Key |
| `YOUTUBE_PROBE_DIR` | Leave as `ydata/user` (default); override to point at a different capture dir |
| `EXTERNAL_DATA_DIR` | Leave as `ydata/guest` (default); override for a different guest data dir |

### Google OAuth credentials (`app_credential/credentials.json`)

Required for `--calendar` and `--youtube`. This is the OAuth Desktop client secrets file from Google Cloud Console — contact Wanli to get added to the Google Cloud project, then:

1. Go to [Google Cloud Console](https://console.cloud.google.com) → your project → APIs & Services → Credentials
2. Download the OAuth 2.0 Desktop client JSON
3. Save it to `app_credential/credentials.json` (gitignored)

### First run

```bash
python -m agents.orchestrator --weather --calendar --youtube
```

Each agent runs its auth flow automatically on first use:

- `--weather`: opens a browser geolocation flow; stores result at `~/.config/radio-podcast/weather_location.json`
- `--calendar`: opens Google OAuth in browser; stores token at `~/.config/radio-podcast/calendar_token.json`
- `--youtube`: opens Google OAuth in browser, then captures your subscriptions/likes to `ydata/user/` (or `$YOUTUBE_PROBE_DIR`)

Subsequent runs skip auth if the stored artifacts exist.

## Usage

```bash
python -m agents.orchestrator --weather --calendar --youtube
```

Absent flag → that agent is skipped. You must pass at least one.

**Other flags:**

- `--no-llm`: skip Producer LLM (segment JSON only; cheap smoke test)
- `--no-external`: skip the external/guest round + agentic-payment step
- `--no-export`: skip the ffmpeg concat at end of run (faster iteration)
- `--user-id <id>`: override default `dev` (isolates feedback signals per user)

**Playback hotkeys (during `afplay` playback):**

- `l` = like (1.10× on that agent's next-run weight)
- `s` = skip to next segment (0.90×)
- `r` = repeat current segment (1.20×)
- `p` / `space` = pause / resume (not a learning signal)
- `q` = quit episode

Feedback signals append to `./data/signals/{user_id}/{episode_id}.jsonl`. Next run hydrates `ProducerMemory` from the log; episode-over-episode re-ordering is visible via the end-of-run weight-delta preview.

**Output locations:**

- Per-segment TTS: `./data/episodes/{episode_id}/segment_{n}.mp3`
- Concat'd episode MP3: `./exports/episode-{episode_id}.mp3`
- Feedback signals: `./data/signals/{user_id}/{episode_id}.jsonl`

## Maintenance

**Re-auth (force re-run of any OAuth flow):**

```bash
rm -f ~/.config/radio-podcast/calendar_token.json \
       ~/.config/radio-podcast/user_profile.json \
       ~/.config/radio-podcast/weather_location.json
rm -rf ydata/user/
```

**Reset agent + producer memory:**

```bash
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

[weights.external]
music = 0.3
```

- Values are clamped to `[0.1, 10.0]`. `1.0` is identity (no effect); `<1.0` damps; `>1.0` boosts.
- Precedence: per-topic entry > category entry > `1.0` default for any topic you don't name.
- Applied as a pure multiplication on each agent's `combined_topic_scores` **before** top-N candidate selection.
- Read once per CLI invocation at orchestrator startup; edits take effect on the next run.

## Architecture

- `agents/orchestrator.py` — top-level runner; flag-driven agent selection + auth preflight
- `agents/weather/`, `agents/calendar/`, `agents/youtube/`, `agents/external/` — per-agent pitch modules
- `pipeline.py` — Producer LLM, segment selection, TTS, ffmpeg export
- `storage/` — per-agent storage helpers (episode dir, signals, agent memory, export)
- `auth/` — OAuth flows for calendar and YouTube
- `learning_loop/` — ProducerMemory hydration from feedback log
- `app_credential/` — Google OAuth client secrets (gitignored)
- `ydata/` — YouTube probe data; `ydata/guest/` is committed, `ydata/user/` is gitignored

> **v1 scope:** Frontend selection UI + web player replace this CLI entirely. See `TODOS.md`.

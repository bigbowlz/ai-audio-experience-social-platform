# audio/config.py
# Voice and model config locked after probe (2026-04-16).
# Probe results: tmp/audio_probe/probe_1776318846/

NARRATOR_VOICE_ID = "SAz9YHcvj6GT2YYXdXww"   # River — picked as narrator (dev's segments)
GUEST_VOICE_ID    = "EXAVITQu4vr4xnSDxMaL"   # Sarah — picked as Alice (@AlicesLens)

ELEVENLABS_MODEL  = "eleven_turbo_v2_5"        # probe p50: 0.95s; flash identical but turbo is design default
OUTPUT_FORMAT     = "mp3_44100_128"             # 22050_32 rated 1/5 on headphones test — dropped

# Orchestrator resolves voice per segment via this map.
# All agents default to narrator. Alice's agent uses guest voice.
VOICE_MAP: dict[str, str] = {
    "youtube":   NARRATOR_VOICE_ID,
    "calendar":  NARRATOR_VOICE_ID,
    "weather":   NARRATOR_VOICE_ID,
    "alices":  GUEST_VOICE_ID,
}

# Voice synthesis settings. Tune stability during rehearsals (0.4/0.6/0.8).
VOICE_SETTINGS: dict = {
    "stability": 0.5,
    "similarity_boost": 0.75,
    "style": 0.0,
    "use_speaker_boost": True,
}

# Per-request timeout (seconds). Spec: 60s per request.
REQUEST_TIMEOUT_SEC = 60

# Retry config for transient errors (429, 5xx).
MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SEC = 1.0  # exponential: 1s, 2s, 4s

# Total pipeline timeout (seconds). If zero segments succeed within this
# window, emit episode.failed.
PIPELINE_TIMEOUT_SEC = 120

# Budget tracking. ElevenLabs Turbo v2.5 pay-as-you-go: ~$0.30/1K chars.
# $20 budget ≈ 66,667 chars. Warn at 80%.
BUDGET_CHAR_LIMIT = 66_667
BUDGET_WARN_THRESHOLD = 0.80  # warn when cumulative chars reach 80% of limit
BUDGET_FILE = "./data/budget.json"

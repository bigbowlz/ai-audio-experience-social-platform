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

"""Audio component — TTS generation, parallel dispatch, offline concat.

Spec: audio/docs/DESIGN.md
"""

from audio.budget import load as load_budget, record as record_budget
from audio.tts import TTSClient, SegmentResult
from audio.orchestrator import generate_episode_audio, AudioResult, OnSegmentDone
from audio.pronunciation import apply_pronunciation
from audio.concat import concat_episode
from audio.events import SegmentDone, SegmentDelayed, EpisodeDone, EpisodeFailed

__all__ = [
    "load_budget",
    "record_budget",
    "TTSClient",
    "SegmentResult",
    "generate_episode_audio",
    "AudioResult",
    "OnSegmentDone",
    "apply_pronunciation",
    "concat_episode",
    "SegmentDone",
    "SegmentDelayed",
    "EpisodeDone",
    "EpisodeFailed",
]

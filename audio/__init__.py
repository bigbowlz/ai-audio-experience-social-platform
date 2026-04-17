"""Audio component — TTS generation, parallel dispatch, offline concat.

Spec: audio/docs/DESIGN.md
"""

from audio.tts import TTSClient, SegmentResult
from audio.orchestrator import generate_episode_audio, AudioResult
from audio.pronunciation import apply_pronunciation
from audio.concat import concat_episode
from audio.events import SegmentDone, SegmentDelayed, EpisodeDone, EpisodeFailed

__all__ = [
    "TTSClient",
    "SegmentResult",
    "generate_episode_audio",
    "AudioResult",
    "apply_pronunciation",
    "concat_episode",
    "SegmentDone",
    "SegmentDelayed",
    "EpisodeDone",
    "EpisodeFailed",
]

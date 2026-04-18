"""End-to-end episode pipeline: producer script streaming -> audio TTS.

Shared composition layer. Called by agents/orchestrator.py CLI and (future)
api-storage. Producer's stream_episode_script yields SegmentScripts one at a
time; audio's generate_episode_audio consumes the iterator, with segment 0
as critical path and 1-N fanning out as the iterator yields.

Spec: docs/specs/2026-04-17-producer-alignment-plan.md Phase 3 (decision 2a)
      producer/docs/DESIGN.md Reviewer Concern #1
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from agents.protocol import Brief, Pitch
from audio.orchestrator import AudioResult, generate_episode_audio
from audio.tts import TTSClient
from producer.script import (
    generate_cold_open,
    generate_sign_off,
    stream_episode_script,
)


@dataclass
class EpisodePipelineResult:
    cold_open: str
    audio: AudioResult
    sign_off: str


async def run_episode_pipeline(
    selected: list[Pitch],
    brief: Brief,
    episode_id: str,
    tts: TTSClient | None = None,
) -> EpisodePipelineResult:
    """Run cold_open -> stream segments into audio -> sign_off.

    If tts is None, constructs one from ELEVENLABS_API_KEY. Callers that want
    to inject a pre-configured or mocked TTSClient (tests, api-storage, a
    future CLI flag) pass tts= explicitly.
    """
    if tts is None:
        tts = TTSClient(api_key=os.environ["ELEVENLABS_API_KEY"])

    cold_open = await generate_cold_open(selected, brief)
    script_iter = stream_episode_script(selected, brief)
    audio_result = await generate_episode_audio(tts, script_iter, episode_id)
    sign_off = await generate_sign_off(brief)

    return EpisodePipelineResult(
        cold_open=cold_open,
        audio=audio_result,
        sign_off=sign_off,
    )

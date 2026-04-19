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
    generate_opener,
    generate_sign_off,
    split_opener_inputs,
    stream_episode_script,
)


@dataclass
class EpisodePipelineResult:
    opener: str
    audio: AudioResult
    sign_off: str


async def run_episode_pipeline(
    selected: list[Pitch],
    brief: Brief,
    episode_id: str,
    tts: TTSClient | None = None,
) -> EpisodePipelineResult:
    """Run opener -> stream content segments into audio -> sign_off.

    Weather and calendar pitches are fused into the single opener LLM call;
    only content segments (alices, youtube, future marketplace agents) reach
    stream_episode_script. If tts is None, constructs one from
    ELEVENLABS_API_KEY.
    """
    if tts is None:
        tts = TTSClient(api_key=os.environ["ELEVENLABS_API_KEY"])

    weather_pitch, calendar_pitch, content_pitches = split_opener_inputs(selected)
    if not content_pitches:
        raise ValueError(
            "run_episode_pipeline: no content pitches after opener split "
            "(running order was weather/calendar only)"
        )

    opener = await generate_opener(
        weather_pitch, calendar_pitch, content_pitches[0], brief
    )
    script_iter = stream_episode_script(content_pitches, brief)
    audio_result = await generate_episode_audio(tts, script_iter, episode_id)
    sign_off = await generate_sign_off(brief)

    return EpisodePipelineResult(
        opener=opener,
        audio=audio_result,
        sign_off=sign_off,
    )

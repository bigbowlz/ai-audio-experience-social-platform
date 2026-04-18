"""Tests for pipeline.run_episode_pipeline — composition verification.

Spec: docs/specs/2026-04-17-producer-alignment-plan.md Phase 3.3b
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from audio.events import EpisodeDone
from audio.orchestrator import AudioResult
from producer.script import SegmentScript


@pytest.mark.asyncio
async def test_pipeline_runs_cold_open_stream_audio_signoff_in_order(monkeypatch):
    """Composition contract: cold_open, then stream→audio, then sign_off."""
    from pipeline import run_episode_pipeline

    call_order: list[str] = []

    async def fake_cold_open(selected, brief):
        call_order.append("cold_open")
        return "Good morning."

    def fake_stream(selected, brief):
        call_order.append("stream_created")

        async def _gen():
            for i, p in enumerate(selected):
                yield SegmentScript(
                    agent=p["agent"],
                    pitch_title=p["title"],
                    segue_in="" if i == 0 else "and next",
                    script="x" * 50,
                    estimated_length_sec=60,
                )
        return _gen()

    async def fake_audio(tts, segments, episode_id, on_segment_done=None):
        call_order.append("audio")
        result = AudioResult()
        async for _ in segments:
            pass
        result.episode_done = EpisodeDone(total_segments=1, skipped_segments=[])
        return result

    async def fake_sign_off(brief):
        call_order.append("sign_off")
        return "Catch you tomorrow."

    monkeypatch.setattr("pipeline.generate_cold_open", fake_cold_open)
    monkeypatch.setattr("pipeline.stream_episode_script", fake_stream)
    monkeypatch.setattr("pipeline.generate_episode_audio", fake_audio)
    monkeypatch.setattr("pipeline.generate_sign_off", fake_sign_off)

    selected = [{
        "agent": "youtube", "title": "Jazz", "hook": "h", "rationale": "r",
        "source_refs": [], "data": {}, "priority": 0.9,
        "thin_signal": False, "claim_kind": "neutral",
        "provenance_shape": "balanced", "suggested_length_sec": 90,
    }]
    brief = {"today_context": {
        "date": "2026-04-17", "day_of_week": "Thursday",
        "time_of_day": "morning", "weather_summary": None,
        "calendar_events": None,
    }}

    result = await run_episode_pipeline(
        selected=selected, brief=brief, episode_id="ep1", tts=MagicMock(),
    )

    assert call_order == ["cold_open", "stream_created", "audio", "sign_off"]
    assert result.cold_open == "Good morning."
    assert result.sign_off == "Catch you tomorrow."
    assert result.audio.episode_done is not None


@pytest.mark.asyncio
async def test_pipeline_constructs_tts_from_env_when_not_provided(monkeypatch):
    """When tts=None and ELEVENLABS_API_KEY is set, TTSClient is constructed."""
    from pipeline import run_episode_pipeline

    captured_tts: list = []

    async def fake_cold_open(selected, brief):
        return "co"

    def fake_stream(selected, brief):
        async def _gen():
            if False:
                yield
        return _gen()

    async def fake_audio(tts, segments, episode_id, on_segment_done=None):
        captured_tts.append(tts)
        return AudioResult()

    async def fake_sign_off(brief):
        return "so"

    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setattr("pipeline.generate_cold_open", fake_cold_open)
    monkeypatch.setattr("pipeline.stream_episode_script", fake_stream)
    monkeypatch.setattr("pipeline.generate_episode_audio", fake_audio)
    monkeypatch.setattr("pipeline.generate_sign_off", fake_sign_off)

    brief = {"today_context": {
        "date": "2026-04-17", "day_of_week": "Thursday",
        "time_of_day": "morning", "weather_summary": None,
        "calendar_events": None,
    }}

    await run_episode_pipeline(selected=[], brief=brief, episode_id="ep1")

    assert len(captured_tts) == 1
    from audio.tts import TTSClient
    assert isinstance(captured_tts[0], TTSClient)

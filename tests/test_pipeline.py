"""Tests for pipeline.run_episode_pipeline — composition verification.

Spec: docs/specs/2026-04-17-producer-alignment-plan.md Phase 3.3b
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from audio.events import EpisodeDone
from audio.orchestrator import AudioResult
from producer.script import SegmentScript


def _pitch(agent: str, title: str, seg_len: int = 90) -> dict:
    return {
        "agent": agent, "title": title, "hook": "h",
        "source_refs": [], "data": {}, "priority": 0.9,
        "thin_signal": False, "claim_kind": "neutral",
        "provenance_shape": "balanced", "suggested_length_sec": seg_len,
    }


@pytest.mark.asyncio
async def test_pipeline_runs_opener_stream_audio_signoff_in_order(monkeypatch):
    """Composition contract: opener, then stream→audio over content pitches, then sign_off."""
    from pipeline import run_episode_pipeline

    call_order: list[str] = []
    opener_args: dict = {}
    stream_args: dict = {}

    async def fake_opener(weather_pitch, calendar_pitch, first_content_pitch, brief):
        call_order.append("opener")
        opener_args["weather"] = weather_pitch
        opener_args["calendar"] = calendar_pitch
        opener_args["first_content"] = first_content_pitch
        return "Good morning, here's your day."

    def fake_stream(selected, brief):
        call_order.append("stream_created")
        stream_args["selected"] = selected

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

    monkeypatch.setattr("pipeline.generate_opener", fake_opener)
    monkeypatch.setattr("pipeline.stream_episode_script", fake_stream)
    monkeypatch.setattr("pipeline.generate_episode_audio", fake_audio)
    monkeypatch.setattr("pipeline.generate_sign_off", fake_sign_off)

    weather = _pitch("weather", "Weather in SF", seg_len=45)
    calendar = _pitch("calendar", "Today's schedule", seg_len=30)
    youtube = _pitch("youtube", "Jazz")
    alices = _pitch("alices", "PG essay")
    selected = [weather, calendar, youtube, alices]
    brief = {"today_context": {
        "date": "2026-04-17", "day_of_week": "Thursday",
        "time_of_day": "morning", "weather_summary": None,
        "calendar_events": None,
    }}

    result = await run_episode_pipeline(
        selected=selected, brief=brief, episode_id="ep1", tts=MagicMock(),
    )

    assert call_order == ["opener", "stream_created", "audio", "sign_off"]
    assert opener_args["weather"] is weather
    assert opener_args["calendar"] is calendar
    assert opener_args["first_content"] is youtube
    # Stream receives only content pitches; weather/calendar fused into opener.
    assert stream_args["selected"] == [youtube, alices]
    assert result.opener == "Good morning, here's your day."
    assert result.sign_off == "Catch you tomorrow."
    assert result.audio.episode_done is not None


@pytest.mark.asyncio
async def test_pipeline_opener_degrades_when_weather_or_calendar_absent(monkeypatch):
    """If weather/calendar not in selected, opener receives None for that input."""
    from pipeline import run_episode_pipeline

    opener_args: dict = {}

    async def fake_opener(weather_pitch, calendar_pitch, first_content_pitch, brief):
        opener_args["weather"] = weather_pitch
        opener_args["calendar"] = calendar_pitch
        opener_args["first_content"] = first_content_pitch
        return "o" * 250

    def fake_stream(selected, brief):
        async def _gen():
            if False:
                yield
        return _gen()

    async def fake_audio(tts, segments, episode_id, on_segment_done=None):
        async for _ in segments:
            pass
        return AudioResult()

    async def fake_sign_off(brief):
        return "so"

    monkeypatch.setattr("pipeline.generate_opener", fake_opener)
    monkeypatch.setattr("pipeline.stream_episode_script", fake_stream)
    monkeypatch.setattr("pipeline.generate_episode_audio", fake_audio)
    monkeypatch.setattr("pipeline.generate_sign_off", fake_sign_off)

    youtube = _pitch("youtube", "Jazz")
    brief = {"today_context": {
        "date": "2026-04-17", "day_of_week": "Thursday",
        "time_of_day": "morning", "weather_summary": None,
        "calendar_events": None,
    }}

    await run_episode_pipeline(
        selected=[youtube], brief=brief, episode_id="ep1", tts=MagicMock(),
    )

    assert opener_args["weather"] is None
    assert opener_args["calendar"] is None
    assert opener_args["first_content"] is youtube


@pytest.mark.asyncio
async def test_pipeline_raises_when_no_content_pitches(monkeypatch):
    """Weather+calendar only → no content segment to speak → fail fast."""
    from pipeline import run_episode_pipeline

    weather = _pitch("weather", "w", seg_len=45)
    calendar = _pitch("calendar", "c", seg_len=30)
    brief = {"today_context": {
        "date": "2026-04-17", "day_of_week": "Thursday",
        "time_of_day": "morning", "weather_summary": None,
        "calendar_events": None,
    }}

    with pytest.raises(ValueError, match="no content pitches"):
        await run_episode_pipeline(
            selected=[weather, calendar], brief=brief,
            episode_id="ep1", tts=MagicMock(),
        )


@pytest.mark.asyncio
async def test_pipeline_constructs_tts_from_env_when_not_provided(monkeypatch):
    """When tts=None and ELEVENLABS_API_KEY is set, TTSClient is constructed."""
    from pipeline import run_episode_pipeline

    captured_tts: list = []

    async def fake_opener(weather_pitch, calendar_pitch, first_content_pitch, brief):
        return "o" * 250

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
    monkeypatch.setattr("pipeline.generate_opener", fake_opener)
    monkeypatch.setattr("pipeline.stream_episode_script", fake_stream)
    monkeypatch.setattr("pipeline.generate_episode_audio", fake_audio)
    monkeypatch.setattr("pipeline.generate_sign_off", fake_sign_off)

    brief = {"today_context": {
        "date": "2026-04-17", "day_of_week": "Thursday",
        "time_of_day": "morning", "weather_summary": None,
        "calendar_events": None,
    }}
    # Need at least one content pitch for pipeline to reach the TTS construction path.
    await run_episode_pipeline(
        selected=[_pitch("youtube", "y")], brief=brief, episode_id="ep1",
    )

    assert len(captured_tts) == 1
    from audio.tts import TTSClient
    assert isinstance(captured_tts[0], TTSClient)

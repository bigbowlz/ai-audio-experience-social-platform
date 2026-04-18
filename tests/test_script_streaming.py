"""Tests for producer/script.py async-iterator surface (Phase 3 / decision 2a).

Spec: producer/docs/DESIGN.md Reviewer Concern #1
      agents/docs/prompt_design.md §4 Step 2
"""
from __future__ import annotations

import asyncio

import pytest

from agents.protocol import Brief, Pitch
from producer.script import (
    SegmentScript,
    generate_segment,           # NEW: per-segment LLM call
    stream_episode_script,      # NEW: AsyncIterator[SegmentScript]
)


def _pitch(agent: str, title: str, seg_len: int = 90) -> Pitch:
    return {
        "agent": agent, "title": title, "hook": "h", "rationale": "r",
        "source_refs": [], "data": {}, "priority": 0.9,
        "thin_signal": False, "claim_kind": "neutral",
        "provenance_shape": "balanced", "suggested_length_sec": seg_len,
    }


def _brief() -> Brief:
    return {"today_context": {
        "date": "2026-04-17", "day_of_week": "Thursday",
        "time_of_day": "morning", "weather_summary": None,
        "calendar_events": None,
    }}


@pytest.mark.asyncio
async def test_stream_emits_segment_one_first(monkeypatch):
    """Decision 2a: segment 0 must arrive before segments 1-N start."""
    selected = [_pitch("youtube", "yt"), _pitch("calendar", "cal")]

    captured: list[str] = []

    async def fake_generate_segment(segment, brief, is_first):
        captured.append("call")
        return SegmentScript(
            agent=segment["agent"], pitch_title=segment["title"],
            segue_in="" if is_first else "And next…",
            script="x" * 50, estimated_length_sec=segment["suggested_length_sec"],
        )

    monkeypatch.setattr("producer.script.generate_segment", fake_generate_segment)

    received: list[SegmentScript] = []
    async for seg in stream_episode_script(selected, _brief()):
        received.append(seg)

    assert len(received) == 2
    assert received[0]["pitch_title"] == "yt"      # first input → first emitted
    assert received[0]["segue_in"] == ""           # first segment has no segue_in
    assert received[1]["segue_in"] != ""


@pytest.mark.asyncio
async def test_stream_validates_each_segment(monkeypatch):
    """Decision 2a: per-segment validation (script length floor) still applies."""
    selected = [_pitch("youtube", "yt")]

    async def too_short(segment, brief, is_first):
        return SegmentScript(
            agent=segment["agent"], pitch_title=segment["title"],
            segue_in="", script="hi", estimated_length_sec=10,
        )

    monkeypatch.setattr("producer.script.generate_segment", too_short)

    with pytest.raises(ValueError, match="too short"):
        async for _ in stream_episode_script(selected, _brief()):
            pass


@pytest.mark.asyncio
async def test_generate_segment_raises_when_disable_llm_set(monkeypatch):
    monkeypatch.setenv("DISABLE_LLM", "1")
    with pytest.raises(RuntimeError, match="DISABLE_LLM"):
        await generate_segment(_pitch("youtube", "yt"), _brief(), is_first=True)

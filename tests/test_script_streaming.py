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
        "agent": agent, "title": title, "hook": "h",
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


from types import SimpleNamespace


def _resp_text(text: str):
    """Mock an Anthropic response whose content is a single text block (no tool use)."""
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


def _segment_json(
    *,
    agent: str = "youtube",
    pitch_title: str = "yt",
    segue_in: str = "",
    script: str = "x" * 50,
    estimated_length_sec: int = 60,
    research_outcome: str = "story",
) -> str:
    import json
    return json.dumps({
        "agent": agent, "pitch_title": pitch_title,
        "segue_in": segue_in, "script": script,
        "estimated_length_sec": estimated_length_sec,
        "research_outcome": research_outcome,
    })


class TestGenerateSegmentToolPlumbing:
    @pytest.mark.asyncio
    async def test_web_search_tool_block_in_create_call(self, monkeypatch, tmp_path):
        """generate_segment passes a web_search_20250305 tool with max_uses=2 and timeout=40s."""
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))
        captured = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            return _resp_text(_segment_json(pitch_title="yt"))

        monkeypatch.setattr("producer.script._client.messages.create", fake_create)

        await generate_segment(_pitch("youtube", "yt"), _brief(), is_first=True)

        assert captured["timeout"] == 40.0
        tools = captured.get("tools")
        assert tools, "generate_segment must pass tools= with the web_search block"
        web = next((t for t in tools if t.get("type", "").startswith("web_search")), None)
        assert web is not None, f"no web_search tool in {tools!r}"
        assert web["max_uses"] == 2

    @pytest.mark.asyncio
    async def test_payload_does_not_leak_source_refs_into_query_seed(
        self, monkeypatch, tmp_path
    ):
        """The user payload carries title + source_refs, but the system prompt owns
        the query-derivation rule. This test pins the payload shape so future
        refactors can't accidentally pre-concatenate source_refs into a `query`
        field that the LLM would then use verbatim."""
        import json as _json
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))
        captured = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            return _resp_text(_segment_json(pitch_title="yt"))

        monkeypatch.setattr("producer.script._client.messages.create", fake_create)

        pitch = _pitch("youtube", "yt")
        pitch["source_refs"] = ["BlueNote", "NationalGeographic"]
        await generate_segment(pitch, _brief(), is_first=True)

        payload = _json.loads(captured["messages"][0]["content"])
        # source_refs stays in the segment block (the LLM needs it for the
        # recitation-avoidance context).
        assert payload["segment"]["source_refs"] == ["BlueNote", "NationalGeographic"]
        # But there is NO top-level "query" or "search_seed" field — the LLM derives its own.
        assert "query" not in payload
        assert "search_seed" not in payload

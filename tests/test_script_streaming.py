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


def _resp_with_tool_blocks(final_text: str):
    """Mock a response whose content list has tool blocks BEFORE the final text."""
    return SimpleNamespace(content=[
        SimpleNamespace(type="server_tool_use", name="web_search", input={"query": "jazz"}),
        SimpleNamespace(type="web_search_tool_result", content=[]),
        SimpleNamespace(type="text", text=final_text),
    ])


def _resp_text_then_more_text(first: str, second: str):
    """Mock a response with two text blocks. We take the last."""
    return SimpleNamespace(content=[
        SimpleNamespace(type="text", text=first),
        SimpleNamespace(type="text", text=second),
    ])


class TestGenerateSegmentMultiBlockParse:
    @pytest.mark.asyncio
    async def test_extracts_final_text_block_after_tool_use(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))

        def fake_create(**kwargs):
            return _resp_with_tool_blocks(_segment_json(pitch_title="yt", script="x" * 50))

        monkeypatch.setattr("producer.script._client.messages.create", fake_create)

        seg = await generate_segment(_pitch("youtube", "yt"), _brief(), is_first=True)
        assert seg["pitch_title"] == "yt"
        assert len(seg["script"]) >= 20

    @pytest.mark.asyncio
    async def test_uses_last_text_block_when_multiple(self, monkeypatch, tmp_path):
        """If the model emits intermediate commentary text then final JSON, take the last."""
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))

        def fake_create(**kwargs):
            return _resp_text_then_more_text(
                "Let me search for that.",
                _segment_json(pitch_title="yt", script="x" * 50),
            )

        monkeypatch.setattr("producer.script._client.messages.create", fake_create)

        seg = await generate_segment(_pitch("youtube", "yt"), _brief(), is_first=True)
        assert seg["pitch_title"] == "yt"

    @pytest.mark.asyncio
    async def test_raises_when_no_text_block(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))

        def fake_create(**kwargs):
            return SimpleNamespace(content=[
                SimpleNamespace(type="server_tool_use", name="web_search", input={}),
            ])

        monkeypatch.setattr("producer.script._client.messages.create", fake_create)

        with pytest.raises(ValueError, match="no text content"):
            await generate_segment(_pitch("youtube", "yt"), _brief(), is_first=True)

    @pytest.mark.asyncio
    async def test_strips_research_outcome_from_yielded_segment(
        self, monkeypatch, tmp_path
    ):
        """research_outcome is telemetry only — never appears on the returned SegmentScript."""
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))

        def fake_create(**kwargs):
            return _resp_text(_segment_json(
                pitch_title="yt", script="x" * 50, research_outcome="story",
            ))

        monkeypatch.setattr("producer.script._client.messages.create", fake_create)

        seg = await generate_segment(_pitch("youtube", "yt"), _brief(), is_first=True)
        assert "research_outcome" not in seg
        assert set(seg.keys()) == {"agent", "pitch_title", "segue_in", "script", "estimated_length_sec"}


from producer.events import EventBus, set_default_bus


class TestResearchFallbackTelemetry:
    @pytest.mark.asyncio
    async def test_hook_fallback_emits_event(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))
        bus = EventBus()
        captured: list[tuple[str, dict]] = []
        bus.subscribe(lambda n, p: captured.append((n, p)))
        set_default_bus(bus)
        try:
            def fake_create(**kwargs):
                return _resp_text(_segment_json(
                    pitch_title="yt", script="x" * 50,
                    research_outcome="hook_fallback",
                ))

            monkeypatch.setattr("producer.script._client.messages.create", fake_create)
            await generate_segment(_pitch("youtube", "yt"), _brief(), is_first=True)
        finally:
            set_default_bus(EventBus())

        events = [(n, p) for n, p in captured
                  if n == "producer.segment.research_fallback"]
        assert len(events) == 1
        name, payload = events[0]
        assert payload["agent"] == "youtube"
        assert payload["pitch_title"] == "yt"
        assert "reason" in payload  # "empty_search" | "broadened_empty"

    @pytest.mark.asyncio
    async def test_story_outcome_does_not_emit_event(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))
        bus = EventBus()
        captured: list[tuple[str, dict]] = []
        bus.subscribe(lambda n, p: captured.append((n, p)))
        set_default_bus(bus)
        try:
            def fake_create(**kwargs):
                return _resp_text(_segment_json(
                    pitch_title="yt", script="x" * 50, research_outcome="story",
                ))
            monkeypatch.setattr("producer.script._client.messages.create", fake_create)
            await generate_segment(_pitch("youtube", "yt"), _brief(), is_first=True)
        finally:
            set_default_bus(EventBus())

        events = [n for n, _ in captured if n == "producer.segment.research_fallback"]
        assert events == []

    @pytest.mark.asyncio
    async def test_hook_fallback_still_enforces_min_script_floor(
        self, monkeypatch, tmp_path
    ):
        """Spec invariant: _MIN_SCRIPT_CHARS (20) still applies to hook-fallback output."""
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))

        def fake_create(**kwargs):
            return _resp_text(_segment_json(
                pitch_title="yt",
                script="too short.",  # 10 chars
                research_outcome="hook_fallback",
            ))

        monkeypatch.setattr("producer.script._client.messages.create", fake_create)

        with pytest.raises(ValueError, match="too short"):
            await generate_segment(_pitch("youtube", "yt"), _brief(), is_first=True)


class TestSegmentCacheIntegration:
    @pytest.mark.asyncio
    async def test_cache_miss_writes_artifact(self, monkeypatch, tmp_path):
        """First call with no cache writes the artifact and emits cache_written."""
        import json as _json
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))

        bus = EventBus()
        captured: list[tuple[str, dict]] = []
        bus.subscribe(lambda n, p: captured.append((n, p)))
        set_default_bus(bus)

        try:
            def fake_create(**kwargs):
                return _resp_text(_segment_json(pitch_title="Jazz", script="x" * 50))

            monkeypatch.setattr("producer.script._client.messages.create", fake_create)

            pitch = _pitch("youtube", "Jazz")
            brief = _brief()
            await generate_segment(pitch, brief, is_first=True)
        finally:
            set_default_bus(EventBus())

        # Artifact present on disk
        date = brief["today_context"]["date"].replace("-", "")
        expected = tmp_path / "segment_scripts" / f"youtube_jazz_{date}_130.json"
        assert expected.exists()
        art = _json.loads(expected.read_text(encoding="utf-8"))
        assert set(art.keys()) == {"segment", "debug"}
        assert art["segment"]["pitch_title"] == "Jazz"
        assert "research_outcome" in art["debug"]
        assert "raw_llm_text" in art["debug"]
        assert "input_pitch" in art["debug"]
        assert art["debug"]["target_words"] == _target_words_helper(pitch["suggested_length_sec"])
        assert art["debug"]["words_per_minute"] == 130

        # cache_written event emitted
        cw = [(n, p) for n, p in captured if n == "producer.segment.cache_written"]
        assert len(cw) == 1
        assert cw[0][1]["agent"] == "youtube"
        assert cw[0][1]["pitch_title"] == "Jazz"
        assert cw[0][1]["cache_path"] == str(expected)

    @pytest.mark.asyncio
    async def test_cache_hit_skips_llm_and_emits_cache_hit(self, monkeypatch, tmp_path):
        """Second call with a matching cache hits and never calls the LLM."""
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))

        def fake_create(**kwargs):
            return _resp_text(_segment_json(pitch_title="Jazz", script="x" * 50))

        monkeypatch.setattr("producer.script._client.messages.create", fake_create)
        pitch = _pitch("youtube", "Jazz")
        brief = _brief()
        # Prime the cache
        await generate_segment(pitch, brief, is_first=True)

        # Now switch the mock to raise — a real call would fail.
        def raising_create(**kwargs):
            raise AssertionError("LLM must not be called on cache hit")

        monkeypatch.setattr("producer.script._client.messages.create", raising_create)

        bus = EventBus()
        captured: list[tuple[str, dict]] = []
        bus.subscribe(lambda n, p: captured.append((n, p)))
        set_default_bus(bus)

        try:
            seg = await generate_segment(pitch, brief, is_first=True)
        finally:
            set_default_bus(EventBus())

        assert seg["pitch_title"] == "Jazz"
        hits = [(n, p) for n, p in captured if n == "producer.segment.cache_hit"]
        assert len(hits) == 1
        assert hits[0][1]["agent"] == "youtube"
        assert hits[0][1]["pitch_title"] == "Jazz"

    @pytest.mark.asyncio
    async def test_cache_hit_different_wpm_is_a_miss(self, monkeypatch, tmp_path):
        """wpm is part of the cache key — changing it invalidates."""
        import json as _json
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))
        monkeypatch.delenv("PRODUCER_WORDS_PER_MIN", raising=False)

        call_count = [0]

        def fake_create(**kwargs):
            call_count[0] += 1
            return _resp_text(_segment_json(pitch_title="Jazz", script="x" * 50))

        monkeypatch.setattr("producer.script._client.messages.create", fake_create)
        pitch = _pitch("youtube", "Jazz")
        brief = _brief()
        await generate_segment(pitch, brief, is_first=True)   # writes wpm=130 file
        assert call_count[0] == 1

        monkeypatch.setenv("PRODUCER_WORDS_PER_MIN", "150")
        await generate_segment(pitch, brief, is_first=True)   # must miss — different wpm
        assert call_count[0] == 2

    @pytest.mark.asyncio
    async def test_cache_write_survives_oserror_on_write(
        self, monkeypatch, tmp_path, capsys
    ):
        """Cache-write failure must not block generation; logs and continues."""
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))

        def fake_create(**kwargs):
            return _resp_text(_segment_json(pitch_title="Jazz", script="x" * 50))

        monkeypatch.setattr("producer.script._client.messages.create", fake_create)

        def bad_write(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr("producer.script._write_cached_artifact", bad_write)

        # Must not raise.
        seg = await generate_segment(_pitch("youtube", "Jazz"), _brief(), is_first=True)
        assert seg["pitch_title"] == "Jazz"


# Small helper for the target_words assertion in the cache artifact test above.
def _target_words_helper(sec: int) -> int:
    from producer.script import _target_words
    return _target_words(sec)

"""Tests for producer/script.py — generate_episode_script() and related surface.

Coverage per docs/specs/2026-04-17-producer-step2-prompt.md §D4:
- Group B — system prompt structural assertions (string-in checks)
- Group C — validation assertions: drop-segments, first-segue-empty, short-script
- Group D — happy path (mocked generate_segment)
"""

from __future__ import annotations

import pytest

from agents.protocol import Brief, Pitch, TodayContext
from producer.script import (
    SYSTEM_PROMPT,
    EpisodeScript,
    SegmentScript,
    generate_episode_script,
    stream_episode_script,
)


# ── Fixtures ──────────────────────────────────────────────────────────


def _full_pitch(
    agent: str = "youtube",
    title: str = "Jazz exploration",
    priority: float = 0.91,
    suggested_length_sec: int = 90,
    claim_kind: str = "rising",
    provenance_shape: str = "balanced",
    thin_signal: bool = False,
    data: dict | None = None,
) -> dict:
    """Build a fully-populated Pitch with every field set."""
    return {
        "agent": agent,
        "title": title,
        "hook": "you've been getting into jazz lately",
        "rationale": "Topic 'jazz' scored 0.91 (combined), claim_kind=rising.",
        "source_refs": ["Blue Note Records", "Coltrane Live at Birdland"],
        "data": data if data is not None else {},
        "priority": priority,
        "claim_kind": claim_kind,
        "provenance_shape": provenance_shape,
        "thin_signal": thin_signal,
        "suggested_length_sec": suggested_length_sec,
    }


_TODAY: TodayContext = {
    "date": "2026-04-17",
    "day_of_week": "Thursday",
    "time_of_day": "morning",
    "weather_summary": "rainy, 12°C",
    "calendar_events": ["Team standup 10:00"],
}


_BRIEF: Brief = {"today_context": _TODAY}


def _seg(
    agent: str,
    title: str,
    segue_in: str = "",
    script: str = "Here's a substantial enough script body for the segment.",
    estimated_length_sec: int = 60,
) -> SegmentScript:
    return SegmentScript(
        agent=agent,
        pitch_title=title,
        segue_in=segue_in,
        script=script,
        estimated_length_sec=estimated_length_sec,
    )


# ── Group B: system prompt structural assertions ──────────────────────


class TestSystemPrompt:
    def test_has_claim_kind_directive_block(self):
        """All 4 claim_kind values + Permitted/Prohibited words appear."""
        for kind in ("durable", "rising", "discovery", "neutral"):
            assert kind in SYSTEM_PROMPT, f"missing claim_kind: {kind!r}"
        assert "Permitted" in SYSTEM_PROMPT
        assert "Prohibited" in SYSTEM_PROMPT

    def test_has_field_legend(self):
        """Every Pitch field name appears in the legend."""
        for field in (
            "hook", "rationale", "source_refs", "data",
            "claim_kind", "provenance_shape", "thin_signal",
            "priority", "suggested_length_sec",
        ):
            assert field in SYSTEM_PROMPT, f"missing field in legend: {field!r}"

    def test_has_per_agent_data_crib(self):
        """Each agent appears in a data-crib context."""
        for agent in ("weather", "calendar", "youtube", "alices"):
            assert agent in SYSTEM_PROMPT, f"missing agent in crib: {agent!r}"
        assert "data.current" in SYSTEM_PROMPT
        assert "data.events" in SYSTEM_PROMPT
        assert "notable_facts" in SYSTEM_PROMPT

    def test_has_thin_signal_handling(self):
        """thin_signal handling block names per-agent nudge phrasings."""
        assert "thin_signal" in SYSTEM_PROMPT
        assert "more personal as your YouTube activity grows" in SYSTEM_PROMPT
        assert "Local forecast wasn't available today" in SYSTEM_PROMPT

    def test_has_hook_data_layering_rule(self):
        """Hook vs data layering rule key phrases present."""
        assert "phrasing ceiling" in SYSTEM_PROMPT
        assert "read-only context" in SYSTEM_PROMPT
        assert "content source" in SYSTEM_PROMPT


# ── Group C: validation assertions ────────────────────────────────────
# Tests call stream_episode_script directly (async layer where validation lives).


class TestValidation:
    @pytest.mark.asyncio
    async def test_first_segment_nonempty_segue_in_raises(self, monkeypatch):
        """First segment with non-empty segue_in raises ValueError."""
        pitches = [_full_pitch(agent="weather", title="Weather in SF")]

        async def fake_generate_segment(segment, brief, is_first):
            return _seg(
                agent="weather",
                title="Weather in SF",
                segue_in="And now, the weather...",  # should be empty for first segment
            )

        monkeypatch.setattr("producer.script.generate_segment", fake_generate_segment)

        with pytest.raises(ValueError, match=r"segue_in"):
            async for _ in stream_episode_script(pitches, _BRIEF):
                pass

    @pytest.mark.asyncio
    async def test_short_script_raises(self, monkeypatch):
        """Segment script shorter than 20 chars raises ValueError naming the segment."""
        pitches = [
            _full_pitch(agent="weather", title="Weather in SF"),
            _full_pitch(agent="youtube", title="Jazz exploration"),
        ]

        calls = [0]

        async def fake_generate_segment(segment, brief, is_first):
            calls[0] += 1
            if calls[0] == 1:
                return _seg(agent="weather", title="Weather in SF")
            return _seg(
                agent="youtube",
                title="Jazz exploration",
                segue_in="And here's some music.",
                script="Hi.",  # too short
            )

        monkeypatch.setattr("producer.script.generate_segment", fake_generate_segment)

        with pytest.raises(ValueError, match=r"Jazz exploration"):
            async for _ in stream_episode_script(pitches, _BRIEF):
                pass

    @pytest.mark.asyncio
    async def test_drops_segment_raises(self, monkeypatch):
        """Iterator whose output_keys miss an input key raises ValueError naming the agent."""
        pitches = [
            _full_pitch(agent="weather", title="Weather in SF"),
            _full_pitch(agent="youtube", title="Jazz exploration"),
        ]

        async def fake_generate_segment(segment, brief, is_first):
            # youtube segment returns a wrong title so output_keys won't match input_keys
            if segment["agent"] == "youtube":
                return _seg(agent="youtube", title="WRONG TITLE")
            return _seg(agent=segment["agent"], title=segment["title"])

        monkeypatch.setattr("producer.script.generate_segment", fake_generate_segment)

        with pytest.raises(ValueError, match=r"youtube"):
            async for _ in stream_episode_script(pitches, _BRIEF):
                pass


# ── Group D: happy path ───────────────────────────────────────────────


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_well_formed_response_passes(self, monkeypatch):
        """A complete, valid 2-segment stream returns successfully with expected shape."""
        pitches = [
            _full_pitch(agent="weather", title="Weather in SF"),
            _full_pitch(agent="youtube", title="Jazz exploration"),
        ]

        segs = [
            _seg(
                agent="weather",
                title="Weather in SF",
                segue_in="",
                script="Currently 55F and rainy in San Francisco. Highs near 60 today.",
                estimated_length_sec=45,
            ),
            _seg(
                agent="youtube",
                title="Jazz exploration",
                segue_in="From the weather, let's pivot to something for your ears.",
                script="You've been getting into jazz lately — Coltrane Live at Birdland turned up in a recent like.",
                estimated_length_sec=90,
            ),
        ]
        idx = [0]

        async def fake_generate_segment(segment, brief, is_first):
            result = segs[idx[0]]
            idx[0] += 1
            return result

        monkeypatch.setattr("producer.script.generate_segment", fake_generate_segment)

        collected: list[SegmentScript] = []
        async for seg in stream_episode_script(pitches, _BRIEF):
            collected.append(seg)

        assert len(collected) == 2
        assert collected[0]["segue_in"] == ""
        assert collected[0]["agent"] == "weather"
        assert collected[1]["agent"] == "youtube"
        assert collected[1]["estimated_length_sec"] == 90

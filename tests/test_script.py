"""Tests for producer/script.py — generate_episode_script() and _format_input().

Coverage per docs/specs/2026-04-17-producer-step2-prompt.md §D4:
- Group A — payload shape (no LLM): _format_input output
- Group B — system prompt structural assertions (string-in checks)
- Group C — validation assertions: drop-segments, first-segue-empty, short-script
- Group D — happy path (mocked LLM)
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agents.protocol import Brief, Pitch, TodayContext
from producer.script import (
    SYSTEM_PROMPT,
    EpisodeScript,
    SegmentScript,
    _format_input,
    generate_episode_script,
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


def _minimal_pitch(agent: str = "youtube", title: str = "Topic") -> dict:
    """Build a Pitch with only required fields — exercises defaults."""
    return {
        "agent": agent,
        "title": title,
        "hook": "hook text",
        "priority": 0.5,
        "suggested_length_sec": 60,
    }


_TODAY: TodayContext = {
    "date": "2026-04-17",
    "day_of_week": "Thursday",
    "time_of_day": "morning",
    "weather_summary": "rainy, 12°C",
    "calendar_events": ["Team standup 10:00"],
}


_BRIEF: Brief = {"today_context": _TODAY}


def _episode_response(segments: list[dict]) -> dict[str, Any]:
    """Build a valid EpisodeScript response payload."""
    return {
        "cold_open": "Good morning. It's a rainy Thursday — let's get into it.",
        "segments": segments,
        "sign_off": "That's the show. Catch you tomorrow.",
    }


def _segment_response(
    agent: str,
    title: str,
    segue_in: str = "",
    script: str = "Here's a substantial enough script body for the segment.",
    estimated_length_sec: int = 60,
) -> dict[str, Any]:
    return {
        "agent": agent,
        "pitch_title": title,
        "segue_in": segue_in,
        "script": script,
        "estimated_length_sec": estimated_length_sec,
    }


def _mock_client(response_data: dict[str, Any]) -> MagicMock:
    """Return a mock Anthropic client whose messages.create returns response_data as JSON text."""
    msg = MagicMock()
    msg.content = [MagicMock(type="text", text=json.dumps(response_data))]
    client = MagicMock()
    client.messages.create.return_value = msg
    return client


# ── Group A: payload shape ────────────────────────────────────────────


class TestFormatInputPayload:
    def test_passes_all_pitch_fields(self):
        """Every selected segment dict carries all 11 Pitch-derived fields."""
        pitch = _full_pitch()
        result = json.loads(_format_input([pitch], _TODAY))
        assert len(result["selected_segments"]) == 1
        seg = result["selected_segments"][0]
        expected_keys = {
            "agent", "title", "hook", "rationale", "source_refs",
            "data", "priority", "claim_kind", "provenance_shape",
            "thin_signal", "suggested_length_sec",
        }
        assert set(seg.keys()) == expected_keys

    def test_top_level_keys_exact(self):
        """Top-level payload has exactly selected_segments, today_context, target_total_secs."""
        result = json.loads(_format_input([_full_pitch()], _TODAY))
        assert set(result.keys()) == {"selected_segments", "today_context", "target_total_secs"}
        # Explicitly assert producer_memory is NOT a key (per spec D1).
        assert "producer_memory" not in result

    def test_defaults_missing_optional_fields(self):
        """Minimal Pitch (only required fields) gets safe defaults for optionals."""
        pitch = _minimal_pitch()
        result = json.loads(_format_input([pitch], _TODAY))
        seg = result["selected_segments"][0]
        assert seg["rationale"] == ""
        assert seg["source_refs"] == []
        assert seg["data"] == {}
        assert seg["claim_kind"] == "neutral"
        assert seg["provenance_shape"] == "balanced"
        assert seg["thin_signal"] is False

    def test_preserves_data_verbatim(self):
        """Weather Pitch with full data round-trips byte-identical (no projection at v0)."""
        weather_data = {
            "current": {"temperature_f": 55, "condition": "rain"},
            "day_ahead": {"high_f": 60, "low_f": 50, "hours_remaining": 12},
            "hourly_forecast": [
                {"hour": h, "temperature_f": 50 + h, "precipitation_probability": 70}
                for h in range(24)
            ],
            "notable_facts": [{"category": "precipitation", "summary": "rain", "severity": "notable"}],
            "air_quality": {"aqi": 35, "category": "fair"},
            "location_name": "San Francisco",
        }
        pitch = _full_pitch(agent="weather", title="Weather in SF", data=weather_data)
        result = json.loads(_format_input([pitch], _TODAY))
        assert result["selected_segments"][0]["data"] == weather_data


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
        # Specific data-field references the crib must call out:
        assert "data.current" in SYSTEM_PROMPT
        assert "data.events" in SYSTEM_PROMPT
        assert "notable_facts" in SYSTEM_PROMPT

    def test_has_thin_signal_handling(self):
        """thin_signal handling block names per-agent nudge phrasings."""
        assert "thin_signal" in SYSTEM_PROMPT
        # YouTube/Alices nudge:
        assert "more personal as your YouTube activity grows" in SYSTEM_PROMPT
        # Weather nudge:
        assert "Local forecast wasn't available today" in SYSTEM_PROMPT

    def test_has_hook_data_layering_rule(self):
        """Hook vs data layering rule key phrases present."""
        assert "phrasing ceiling" in SYSTEM_PROMPT
        assert "read-only context" in SYSTEM_PROMPT
        assert "content source" in SYSTEM_PROMPT


# ── Group C: validation assertions ────────────────────────────────────


class TestValidation:
    def test_first_segment_nonempty_segue_in_raises(self):
        """First segment with non-empty segue_in raises ValueError."""
        pitches = [_full_pitch(agent="weather", title="Weather in SF")]
        bad_response = _episode_response([
            _segment_response(
                agent="weather",
                title="Weather in SF",
                segue_in="And now, the weather...",  # should be empty for first segment
            ),
        ])
        with patch("producer.script.anthropic.Anthropic", return_value=_mock_client(bad_response)):
            with pytest.raises(ValueError, match=r"segue_in"):
                generate_episode_script(pitches, _BRIEF)

    def test_short_script_raises(self):
        """Segment script shorter than 20 chars raises ValueError naming the segment."""
        pitches = [
            _full_pitch(agent="weather", title="Weather in SF"),
            _full_pitch(agent="youtube", title="Jazz exploration"),
        ]
        bad_response = _episode_response([
            _segment_response(agent="weather", title="Weather in SF"),
            _segment_response(
                agent="youtube",
                title="Jazz exploration",
                segue_in="And here's some music.",
                script="Hi.",  # too short
            ),
        ])
        with patch("producer.script.anthropic.Anthropic", return_value=_mock_client(bad_response)):
            with pytest.raises(ValueError, match=r"Jazz exploration"):
                generate_episode_script(pitches, _BRIEF)

    def test_drops_segment_raises(self):
        """LLM response missing a segment raises ValueError naming the dropped agent."""
        pitches = [
            _full_pitch(agent="weather", title="Weather in SF"),
            _full_pitch(agent="youtube", title="Jazz exploration"),
        ]
        # Response only contains weather; youtube is dropped.
        bad_response = _episode_response([
            _segment_response(agent="weather", title="Weather in SF"),
        ])
        with patch("producer.script.anthropic.Anthropic", return_value=_mock_client(bad_response)):
            with pytest.raises(ValueError, match=r"youtube"):
                generate_episode_script(pitches, _BRIEF)

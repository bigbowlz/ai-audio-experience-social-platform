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

"""CLI flag parsing tests for the v0 CLI pivot.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Task 0.2
"""
from __future__ import annotations

import pytest

from agents.orchestrator import _select_internal_agent_classes


def test_weather_flag_selects_weather_only():
    names = _select_internal_agent_classes(
        weather=True, calendar=False, youtube=False
    )
    assert names == ["weather"]


def test_all_three_flags_selects_all_three_in_fixed_order():
    names = _select_internal_agent_classes(
        weather=True, calendar=True, youtube=True
    )
    # Fixed order: weather, calendar, youtube (matches current
    # hardcoded list at agents/orchestrator.py:226 pre-pivot).
    assert names == ["weather", "calendar", "youtube"]


def test_zero_flags_raises_systemexit():
    with pytest.raises(SystemExit):
        _select_internal_agent_classes(
            weather=False, calendar=False, youtube=False
        )


def test_calendar_plus_youtube_skips_weather():
    names = _select_internal_agent_classes(
        weather=False, calendar=True, youtube=True
    )
    assert names == ["calendar", "youtube"]

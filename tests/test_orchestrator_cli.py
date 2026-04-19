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
    with pytest.raises(SystemExit) as exc_info:
        _select_internal_agent_classes(
            weather=False, calendar=False, youtube=False
        )
    # Guard against a silent SystemExit(0) regression — the message is a
    # user-facing contract.
    assert "--weather" in str(exc_info.value)
    assert "--help" in str(exc_info.value)


def test_calendar_plus_youtube_skips_weather():
    names = _select_internal_agent_classes(
        weather=False, calendar=True, youtube=True
    )
    assert names == ["calendar", "youtube"]


def test_youtube_only_flag_selects_youtube_only():
    # Pins the invariant that "youtube being last in the fixed order" is
    # preserved — absent predecessors do not pull youtube forward.
    names = _select_internal_agent_classes(
        weather=False, calendar=False, youtube=True
    )
    assert names == ["youtube"]


def test_cli_main_calls_preflight_once_per_active_agent(monkeypatch):
    """Every activated agent triggers its preflight exactly once, before
    the agent is instantiated. Preserves the weather→calendar→youtube order."""
    import agents.orchestrator as orch

    calls: list[str] = []
    monkeypatch.setattr(
        "auth.preflight.ensure_agent_auth",
        lambda name: calls.append(name),
    )

    # Stub the heavy path: replace run_episode + everything after it.
    monkeypatch.setattr(
        orch, "run_episode",
        lambda *a, **k: ({}, {"today_context": {}, "user_profile": None}),
    )
    # Also stub subscribe so we don't spam stdout with JsonlSink output.
    monkeypatch.setattr("producer.events.subscribe", lambda *_a, **_k: None)
    # Force an exception downstream so the raises guard is satisfied; we only
    # care that preflight was invoked before this point.
    monkeypatch.setattr(
        "producer.memory.load_producer_memory",
        lambda *_a, **_k: (_ for _ in ()).throw(KeyError("stubbed")),
    )

    # SystemExit is acceptable: downstream producer.memory / bonus / script
    # modules are stubbed-away via run_episode returning empty dicts, which
    # causes various KeyErrors downstream. We only assert preflight order.
    with pytest.raises((SystemExit, KeyError, IndexError, AttributeError, TypeError)):
        orch.cli_main(["--weather", "--calendar", "--no-llm", "--no-external"])
    assert calls == ["weather", "calendar"]

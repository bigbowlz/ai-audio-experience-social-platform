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
    the agent is instantiated. Preserves the weather→calendar→youtube order.

    Patches `agents.orchestrator.ensure_agent_auth` directly (the name as it
    is bound inside the module after the top-level import) so the test isn't
    relying on sys.modules cache side effects.
    """
    from unittest.mock import Mock
    import agents.orchestrator as orch

    calls: list[str] = []
    monkeypatch.setattr(
        orch, "ensure_agent_auth", lambda name: calls.append(name)
    )

    # Stub run_episode + subscribe so we don't actually hit agents / TTS /
    # the producer LLM. run_episode's stubbed return feeds empty dicts into
    # downstream code — we force that downstream code to short-circuit
    # cleanly via a controlled KeyError (see next monkeypatch).
    monkeypatch.setattr(
        orch, "run_episode",
        lambda *a, **k: ({}, {"today_context": {}, "user_profile": None}),
    )
    monkeypatch.setattr("producer.events.subscribe", lambda *_a, **_k: None)
    # Controlled downstream short-circuit: the first load after preflight is
    # load_producer_memory. A mock with side_effect raises cleanly on call.
    monkeypatch.setattr(
        "producer.memory.load_producer_memory",
        Mock(side_effect=KeyError("stubbed — test short-circuit after preflight")),
    )

    # Narrow exception type: only KeyError is expected from our stub. A
    # wider tuple here would mask genuine TypeError/AttributeError bugs
    # in the preflight wiring (e.g., ensure_agent_auth called with the
    # wrong arg type).
    with pytest.raises(KeyError, match="stubbed"):
        orch.cli_main(["--weather", "--calendar", "--no-llm", "--no-external"])
    # Assertion runs outside the raises block, after cli_main raises — but
    # the preflight loop fires BEFORE load_producer_memory, so `calls` is
    # fully populated by the time the KeyError fires.
    assert calls == ["weather", "calendar"]


def test_cli_main_hydrates_producer_memory_at_startup(monkeypatch):
    """Hydration runs after preflight but before the banner."""
    from unittest.mock import Mock
    import agents.orchestrator as orch

    calls: list[str] = []
    # Patch the module-level binding (not the source module) so the stub
    # is stable across any future import-ordering changes.
    monkeypatch.setattr(
        orch, "hydrate_producer_memory",
        lambda user_id: (calls.append(user_id), {})[1],
    )
    monkeypatch.setattr(orch, "ensure_agent_auth", lambda _n: None)
    monkeypatch.setattr(
        orch, "run_episode",
        lambda *a, **k: ({}, {"today_context": {}, "user_profile": None}),
    )
    monkeypatch.setattr("producer.events.subscribe", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "producer.memory.load_producer_memory",
        Mock(side_effect=KeyError("stubbed — short-circuit after hydrate")),
    )

    with pytest.raises(KeyError, match="stubbed"):
        orch.cli_main(["--weather", "--no-llm", "--no-external",
                       "--user-id", "demo"])
    assert calls == ["demo"]

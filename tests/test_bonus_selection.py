"""Tests for producer/bonus.py — select_bonus_segments_llm().

Coverage per prompt_design.md §Test mandate (Step 1.5):
- LLM success path: bonus picks accepted within budget
- LLM success path: title-mismatch logged and skipped
- LLM success path: cheap pitch after expensive miss still accepted
- Fallback path (LLM timeout): all guaranteed slots get reasons
- Fallback path (LLM timeout): bonus filled by priority-sort
- Fallback path (LLM timeout): reasoning_summary is "{agent}: {title}"
- Deterministic output for same inputs (ProducerMemory already applied upstream)
- Budget gate: over-budget pitch rejected without blocking next pick

Note: `producer_memory` is no longer a parameter to select_bonus_segments_llm.
ProducerMemory is applied deterministically by apply_producer_memory() before
pitches reach this module. See tests/test_producer_memory.py.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agents.protocol import TodayContext
from producer.bonus import (
    BonusPick,
    BonusSelectionResult,
    PickReason,
    select_bonus_segments_llm,
)


# ── Fixtures ──────────────────────────────────────────────────────────

def _pitch(agent: str, title: str, priority: float, seg_len: int = 90) -> dict:
    """Build a minimal pitch dict (TypedDict is just a dict at runtime)."""
    return {
        "agent": agent,
        "title": title,
        "hook": "hook text",
        "rationale": "test",
        "source_refs": [],
        "priority": priority,
        "thin_signal": False,
        "claim_kind": "neutral",
        "provenance_shape": "balanced",
        "suggested_length_sec": seg_len,
    }


_TODAY: TodayContext = {
    "date": "2026-04-17",
    "day_of_week": "Thursday",
    "time_of_day": "morning",
    "weather_summary": "rainy, 12°C",
    "calendar_events": ["Team standup 10am"],
}


def _bonus_result(
    guaranteed: list[dict],
    bonus_titles: list[str],
    bonus_agents: list[str],
) -> dict[str, Any]:
    """Build a BonusSelectionResult JSON payload for LLM mock responses."""
    return {
        "overall_reasoning": "rainy morning → introspective picks",
        "guaranteed_pick_reasons": [
            {
                "pitch_title": p["title"],
                "agent": p["agent"],
                "reasoning_summary": f"{p['agent']}: strong signal",
            }
            for p in guaranteed
        ],
        "bonus_picks": [
            {
                "pitch_title": title,
                "agent": agent,
                "reasoning_summary": f"{agent}: bonus energy",
            }
            for title, agent in zip(bonus_titles, bonus_agents)
        ],
    }


def _mock_client(response_data: dict[str, Any]) -> MagicMock:
    """Return a mock Anthropic client that returns response_data as JSON."""
    msg = MagicMock()
    msg.content = [MagicMock(type="text", text=json.dumps(response_data))]
    client = MagicMock()
    client.messages.create.return_value = msg
    return client


def _timeout_client(n_failures: int = 2) -> MagicMock:
    """Return a mock Anthropic client that always raises a timeout exception."""
    client = MagicMock()
    client.messages.create.side_effect = [
        Exception(f"timeout attempt {i + 1}") for i in range(n_failures)
    ]
    return client


# ── LLM success path ──────────────────────────────────────────────────

class TestLLMSuccessPath:
    def test_bonus_picks_accepted_within_budget(self):
        # seg_len=40 + 10 segue = 50 cost; budget=55 → fits
        guaranteed = [_pitch("youtube", "Jazz exploration", 0.91, seg_len=90)]
        remaining = [_pitch("youtube", "Web3 skepticism", 0.72, seg_len=40)]
        budget = 55

        mock = _mock_client(_bonus_result(guaranteed, ["Web3 skepticism"], ["youtube"]))
        with patch("producer.bonus.anthropic.Anthropic", return_value=mock):
            bonus, _, _ = select_bonus_segments_llm(
                guaranteed_slots=guaranteed,
                remaining_pitches=remaining,
                budget_remaining_sec=budget,
                today_context=_TODAY,
            )

        assert len(bonus) == 1
        assert bonus[0]["title"] == "Web3 skepticism"
        assert bonus[0]["reasoning_summary"] == "youtube: bonus energy"

    def test_title_mismatch_logged_and_skipped(self, caplog):
        guaranteed = [_pitch("youtube", "Jazz exploration", 0.91, seg_len=90)]
        remaining = [_pitch("youtube", "Web3 skepticism", 0.72, seg_len=40)]
        budget = 100

        # LLM returns a title not in remaining_pitches
        mock = _mock_client(
            _bonus_result(guaranteed, ["Nonexistent Topic"], ["youtube"])
        )
        with patch("producer.bonus.anthropic.Anthropic", return_value=mock):
            bonus, _, _ = select_bonus_segments_llm(
                guaranteed_slots=guaranteed,
                remaining_pitches=remaining,
                budget_remaining_sec=budget,
                today_context=_TODAY,
            )

        assert bonus == []

    def test_cheap_pitch_accepted_after_expensive_miss(self):
        # expensive: seg_len=80 + 10 = 90 cost; budget=50 → too large
        # cheap: seg_len=35 + 10 = 45 cost; budget=50 → fits
        guaranteed = [_pitch("youtube", "Jazz exploration", 0.91, seg_len=90)]
        remaining = [
            _pitch("youtube", "Expensive topic", 0.80, seg_len=80),
            _pitch("youtube", "Cheap topic", 0.60, seg_len=35),
        ]
        budget = 50

        mock = _mock_client(
            _bonus_result(
                guaranteed,
                ["Expensive topic", "Cheap topic"],
                ["youtube", "youtube"],
            )
        )
        with patch("producer.bonus.anthropic.Anthropic", return_value=mock):
            bonus, _, _ = select_bonus_segments_llm(
                guaranteed_slots=guaranteed,
                remaining_pitches=remaining,
                budget_remaining_sec=budget,
                today_context=_TODAY,
            )

        titles = [b["title"] for b in bonus]
        assert "Expensive topic" not in titles
        assert "Cheap topic" in titles

    def test_guaranteed_reasons_returned_for_all_slots(self):
        guaranteed = [
            _pitch("youtube", "Jazz exploration", 0.91, seg_len=90),
            _pitch("weather", "Rainy morning", 0.80, seg_len=45),
        ]
        mock = _mock_client(_bonus_result(guaranteed, [], []))
        with patch("producer.bonus.anthropic.Anthropic", return_value=mock):
            _, guaranteed_reasons, _ = select_bonus_segments_llm(
                guaranteed_slots=guaranteed,
                remaining_pitches=[],
                budget_remaining_sec=0,
                today_context=_TODAY,
            )

        assert len(guaranteed_reasons) == 2
        titles = {r["pitch_title"] for r in guaranteed_reasons}
        assert titles == {"Jazz exploration", "Rainy morning"}


# ── Fallback path (LLM timeout) ───────────────────────────────────────

class TestFallbackPath:
    def _run_with_timeout(
        self,
        guaranteed: list[dict],
        remaining: list[dict],
        budget: int,
    ) -> tuple[list[dict], list[PickReason], str]:
        mock = _timeout_client(n_failures=2)
        with patch("producer.bonus.anthropic.Anthropic", return_value=mock):
            return select_bonus_segments_llm(
                guaranteed_slots=guaranteed,
                remaining_pitches=remaining,
                budget_remaining_sec=budget,
                today_context=_TODAY,
            )

    def test_guaranteed_slots_all_get_reasons_on_fallback(self):
        guaranteed = [
            _pitch("youtube", "Jazz exploration", 0.91, seg_len=90),
            _pitch("weather", "Rainy morning", 0.80, seg_len=45),
            _pitch("calendar", "Standup prep", 0.70, seg_len=30),
            _pitch("alices", "PG essay", 0.85, seg_len=90),
        ]
        _, guaranteed_reasons, _ = self._run_with_timeout(guaranteed, [], 0)

        assert len(guaranteed_reasons) == 4
        agents = {r["agent"] for r in guaranteed_reasons}
        assert agents == {"youtube", "weather", "calendar", "alices"}

    def test_fallback_bonus_filled_by_priority_sort(self):
        guaranteed = [_pitch("youtube", "Jazz exploration", 0.91, seg_len=90)]
        remaining = [
            _pitch("youtube", "Low priority", 0.20, seg_len=30),
            _pitch("youtube", "High priority", 0.80, seg_len=30),
        ]
        # 30 + 10 = 40 cost; budget = 50 → exactly one fits
        budget = 50

        bonus, _, _ = self._run_with_timeout(guaranteed, remaining, budget)

        assert len(bonus) == 1
        assert bonus[0]["title"] == "High priority"

    def test_fallback_guaranteed_reasoning_summary_format(self):
        guaranteed = [_pitch("youtube", "Jazz exploration", 0.91, seg_len=90)]
        _, guaranteed_reasons, _ = self._run_with_timeout(guaranteed, [], 0)

        assert guaranteed_reasons[0]["reasoning_summary"] == "youtube: guaranteed slot"

    def test_fallback_bonus_reasoning_summary_format(self):
        guaranteed = [_pitch("youtube", "Jazz exploration", 0.91, seg_len=90)]
        remaining = [_pitch("alices", "PG essay", 0.85, seg_len=30)]
        budget = 50

        bonus, _, _ = self._run_with_timeout(guaranteed, remaining, budget)

        assert len(bonus) == 1
        # Fallback format: "{agent}: {title}"
        assert bonus[0]["reasoning_summary"] == "alices: PG essay"


# ── Determinism ───────────────────────────────────────────────────────

class TestDeterminism:
    def test_deterministic_output_for_same_inputs(self):
        """Two calls with identical inputs produce identical output."""
        guaranteed = [_pitch("youtube", "Jazz exploration", 0.91, seg_len=90)]
        remaining = [_pitch("alices", "PG essay", 0.85, seg_len=30)]
        budget = 50
        response = _bonus_result(guaranteed, ["PG essay"], ["alices"])

        results = []
        for _ in range(2):
            mock = _mock_client(response)
            with patch("producer.bonus.anthropic.Anthropic", return_value=mock):
                bonus, reasons, _ = select_bonus_segments_llm(
                    guaranteed_slots=guaranteed,
                    remaining_pitches=remaining,
                    budget_remaining_sec=budget,
                    today_context=_TODAY,
                )
            results.append(([b["title"] for b in bonus], [r["pitch_title"] for r in reasons]))

        assert results[0] == results[1]


# ── Budget gate ───────────────────────────────────────────────────────

class TestBudgetGate:
    def test_over_budget_pitch_rejected_without_blocking_next(self):
        guaranteed = [_pitch("youtube", "Jazz exploration", 0.91, seg_len=90)]
        remaining = [
            _pitch("youtube", "Over budget topic", 0.80, seg_len=80),
            _pitch("youtube", "Fits in budget", 0.60, seg_len=30),
        ]
        # over budget: 80 + 10 = 90 > 50; next: 30 + 10 = 40 ≤ 50 → fits
        budget = 50

        mock = _mock_client(
            _bonus_result(
                guaranteed,
                ["Over budget topic", "Fits in budget"],
                ["youtube", "youtube"],
            )
        )
        with patch("producer.bonus.anthropic.Anthropic", return_value=mock):
            bonus, _, _ = select_bonus_segments_llm(
                guaranteed_slots=guaranteed,
                remaining_pitches=remaining,
                budget_remaining_sec=budget,
                today_context=_TODAY,
            )

        titles = [b["title"] for b in bonus]
        assert "Over budget topic" not in titles
        assert "Fits in budget" in titles

    def test_zero_budget_no_bonus_accepted(self):
        guaranteed = [_pitch("youtube", "Jazz exploration", 0.91, seg_len=90)]
        remaining = [_pitch("youtube", "Any topic", 0.80, seg_len=30)]

        mock = _mock_client(_bonus_result(guaranteed, ["Any topic"], ["youtube"]))
        with patch("producer.bonus.anthropic.Anthropic", return_value=mock):
            bonus, _, _ = select_bonus_segments_llm(
                guaranteed_slots=guaranteed,
                remaining_pitches=remaining,
                budget_remaining_sec=0,
                today_context=_TODAY,
            )

        assert bonus == []


# ── Fallback tie-break determinism ────────────────────────────────────

class TestFallbackTieBreakDeterminism:
    """Decision 5a: fallback path picks ties by (-priority, agent, title)."""

    def test_fallback_resolves_cross_agent_ties_by_agent_asc(self, monkeypatch):
        # Force fallback path.
        monkeypatch.setenv("DISABLE_LLM", "1")
        guaranteed: list[dict] = []
        # Two pitches, identical priority, different agents.
        # Budget allows ONE bonus pick.
        remaining = [
            _pitch("youtube", "y1", 0.5, seg_len=40),
            _pitch("calendar", "c1", 0.5, seg_len=40),
        ]
        from producer.bonus import select_bonus_segments_llm
        bonus, _, _ = select_bonus_segments_llm(
            guaranteed_slots=guaranteed,
            remaining_pitches=remaining,
            budget_remaining_sec=50,  # exactly one (40 + 10 segue)
            today_context={
                "date": "2026-04-17", "day_of_week": "Thursday",
                "time_of_day": "morning", "weather_summary": None,
                "calendar_events": None,
            },
        )
        assert len(bonus) == 1
        # calendar < youtube alphabetically, so calendar wins.
        assert bonus[0]["agent"] == "calendar"


class TestStepOnePointFiveSSE:
    """Decision 3d: producer.selecting.{started,done} + producer.pick events."""

    def test_emits_started_then_picks_then_done(self, monkeypatch):
        monkeypatch.setenv("DISABLE_LLM", "1")
        from producer.events import EventBus, set_default_bus
        from producer.bonus import select_bonus_with_events
        bus = EventBus()
        captured = []
        bus.subscribe(lambda n, p: captured.append((n, p)))
        set_default_bus(bus)
        guaranteed = [_pitch("youtube", "yt-1", 0.9, seg_len=90)]
        remaining = [_pitch("youtube", "yt-2", 0.7, seg_len=40)]
        select_bonus_with_events(
            guaranteed_slots=guaranteed,
            remaining_pitches=remaining,
            budget_remaining_sec=50,
            today_context={
                "date": "2026-04-17", "day_of_week": "Thursday",
                "time_of_day": "morning", "weather_summary": None,
                "calendar_events": None,
            },
        )
        names = [n for n, _ in captured]
        assert names[0] == "producer.selecting.started"
        assert names[-1] == "producer.selecting.done"
        assert all(n == "producer.pick" for n in names[1:-1])
        assert len(names) == 1 + 1 + 1 + 1  # started + 1 guaranteed + 1 bonus + done

    def test_selecting_started_forwards_llm_overall_reasoning_verbatim(self):
        """Regression: producer.selecting.started must carry the LLM's
        overall_reasoning verbatim, not a slice of the first guarantee.

        Spec: agents/docs/prompt_design.md:510-513.
        """
        from producer.bonus import select_bonus_with_events
        from producer.events import EventBus, set_default_bus

        bus = EventBus()
        captured: list[tuple[str, dict]] = []
        bus.subscribe(lambda n, p: captured.append((n, p)))
        set_default_bus(bus)

        guaranteed = [_pitch("youtube", "Jazz exploration", 0.91, seg_len=90)]
        remaining = [_pitch("youtube", "Web3 skepticism", 0.72, seg_len=40)]
        distinctive = "rainy morning -> introspective picks"
        payload = _bonus_result(guaranteed, ["Web3 skepticism"], ["youtube"])
        payload["overall_reasoning"] = distinctive

        mock = _mock_client(payload)
        with patch("producer.bonus.anthropic.Anthropic", return_value=mock):
            select_bonus_with_events(
                guaranteed_slots=guaranteed,
                remaining_pitches=remaining,
                budget_remaining_sec=60,
                today_context=_TODAY,
            )

        started = [p for n, p in captured if n == "producer.selecting.started"]
        assert len(started) == 1
        assert started[0]["reasoning_summary"] == distinctive
        # Guarantee reasoning_summary strings must NOT leak into the started event.
        assert "strong signal" not in started[0]["reasoning_summary"]

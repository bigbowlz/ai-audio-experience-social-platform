"""Tests for agents.orchestrator — Brief assembly and time-of-day logic.

Covers the fix: Brief.today_context.time_of_day must use local wall-clock
time, not UTC, so a 15:25 local / 20:25 UTC slot reads "afternoon" not "evening".
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from agents.orchestrator import _time_of_day, run_episode
from agents.protocol import AgentMemory, Brief, Pitch, ScopeContext, bootstrap_memory


# ── _time_of_day unit tests ───────────────────────────────────────────

class TestTimeOfDay:
    @pytest.mark.parametrize("hour,expected", [
        (0,  "night"),
        (4,  "night"),
        (5,  "morning"),
        (11, "morning"),
        (12, "afternoon"),
        (16, "afternoon"),
        (17, "evening"),
        (20, "evening"),
        (21, "night"),
        (23, "night"),
    ])
    def test_boundaries(self, hour: int, expected: str) -> None:
        assert _time_of_day(hour) == expected

    def test_afternoon_not_evening_at_15(self) -> None:
        """15:00 local must be 'afternoon', not 'evening' (regression: was using UTC hour)."""
        assert _time_of_day(15) == "afternoon"


# ── run_episode Brief assembly ────────────────────────────────────────

def _make_stub_agent(name: str, pitches: list[Pitch] | None = None) -> MagicMock:
    """Return a DataAgent stub that returns empty context and one neutral pitch."""
    agent = MagicMock()
    agent.name = name
    agent.load_memory.return_value = bootstrap_memory()
    agent.fetch_context.return_value = ScopeContext()
    agent.pitch.return_value = pitches or [
        Pitch(
            agent=name,
            title=f"{name} title",
            hook=f"{name} hook",
            data={},
            rationale="",
            source_refs=[],
            priority=0.5,
            thin_signal=False,
            claim_kind="neutral",
            provenance_shape="balanced",
        )
    ]
    return agent


class TestRunEpisodeBriefUsesLocalTime:
    def test_time_of_day_uses_local_not_utc(self) -> None:
        """Brief.today_context.time_of_day reflects local wall-clock hour.

        Simulates a user at UTC+5 where local hour is 15 (afternoon) but UTC
        hour would be 20 (evening). Brief must report 'afternoon'.
        """
        local_afternoon = datetime(2026, 4, 17, 15, 25, 0)  # naive = local

        agents = [_make_stub_agent("weather"), _make_stub_agent("calendar")]

        with patch("agents.orchestrator.datetime") as mock_dt:
            mock_dt.now.return_value = local_afternoon
            mock_dt.now.side_effect = None
            # strftime / strptime still need to work normally
            mock_dt.strftime = datetime.strftime

            pitches, brief = run_episode(agents, user_id="test")

        assert brief["today_context"]["time_of_day"] == "afternoon"

    def test_brief_date_matches_local_date(self) -> None:
        """Brief.today_context.date is the local calendar date, not UTC date."""
        local_night = datetime(2026, 4, 17, 23, 50, 0)  # late local night — still Apr 17

        agents = [_make_stub_agent("weather")]

        with patch("agents.orchestrator.datetime") as mock_dt:
            mock_dt.now.return_value = local_night
            mock_dt.now.side_effect = None
            mock_dt.strftime = datetime.strftime

            _, brief = run_episode(agents, user_id="test")

        assert brief["today_context"]["date"] == "2026-04-17"

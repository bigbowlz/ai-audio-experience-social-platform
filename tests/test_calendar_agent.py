"""Tests for CalendarAgent — fetch_context + pitch.

Mock boundary: agents.calendar.agent._list_events
All Google API interaction is behind that single function.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from agents.calendar.agent import CalendarAgent
from agents.protocol import bootstrap_memory, Brief, TodayContext


@pytest.fixture
def agent() -> CalendarAgent:
    return CalendarAgent()


# ── fetch_context tests ──────────────────────────────────────────────


class TestFetchContext:
    """Tests for CalendarAgent.fetch_context()."""

    def test_api_reachable_with_events(self, agent: CalendarAgent):
        """Normal path: API returns events, context has flat + rich dicts."""
        fake_events = [
            {
                "summary": "Team standup",
                "start": {"dateTime": "2026-04-16T10:00:00+08:00"},
                "end": {"dateTime": "2026-04-16T10:30:00+08:00"},
                "attendees": [{"email": "a@x.com"}, {"email": "b@x.com"}],
                "recurringEventId": "abc123",
                "conferenceData": {"entryPoints": [{"entryPointType": "video"}]},
                "organizer": {"displayName": "Alex Chen"},
                "status": "confirmed",
            },
        ]
        with patch("agents.calendar.agent._load_credentials", return_value=MagicMock()), \
             patch("agents.calendar.agent._list_events", return_value=fake_events):
            ctx = agent.fetch_context("dev")

        assert ctx["api_reachable"] is True
        assert len(ctx["calendar_events"]) == 1
        assert "Team standup" in ctx["calendar_events"][0]
        assert "10:00" in ctx["calendar_events"][0]

        rich = ctx["calendar_events_rich"]
        assert len(rich) == 1
        assert rich[0]["summary"] == "Team standup"
        assert rich[0]["start"] == "10:00"
        assert rich[0]["end"] == "10:30"
        assert rich[0]["duration_min"] == 30
        assert rich[0]["attendee_count"] == 2
        assert rich[0]["is_recurring"] is True
        assert rich[0]["has_video_call"] is True
        assert rich[0]["organizer"] == "Alex Chen"

    def test_api_reachable_no_events(self, agent: CalendarAgent):
        """API succeeds but returns empty list."""
        with patch("agents.calendar.agent._load_credentials", return_value=MagicMock()), \
             patch("agents.calendar.agent._list_events", return_value=[]):
            ctx = agent.fetch_context("dev")

        assert ctx["api_reachable"] is True
        assert ctx["calendar_events"] == []
        assert ctx["calendar_events_rich"] == []

    def test_api_unreachable_no_credentials(self, agent: CalendarAgent):
        """Token missing/revoked — _load_credentials returns None."""
        with patch("agents.calendar.agent._load_credentials", return_value=None):
            ctx = agent.fetch_context("dev")

        assert ctx["api_reachable"] is False
        assert ctx["calendar_events"] == []
        assert ctx["calendar_events_rich"] == []

    def test_api_unreachable_list_events_fails(self, agent: CalendarAgent):
        """Credentials OK but _list_events returns None (API failure)."""
        with patch("agents.calendar.agent._load_credentials", return_value=MagicMock()), \
             patch("agents.calendar.agent._list_events", return_value=None):
            ctx = agent.fetch_context("dev")

        assert ctx["api_reachable"] is False
        assert ctx["calendar_events"] == []
        assert ctx["calendar_events_rich"] == []

    def test_malformed_datetime_skipped(self, agent: CalendarAgent):
        """Event with unparseable datetime is skipped, others kept."""
        fake_events = [
            {
                "summary": "Good event",
                "start": {"dateTime": "2026-04-16T09:00:00+08:00"},
                "end": {"dateTime": "2026-04-16T09:30:00+08:00"},
                "status": "confirmed",
            },
            {
                "summary": "Bad event",
                "start": {"dateTime": "not-a-date"},
                "end": {"dateTime": "also-bad"},
                "status": "confirmed",
            },
        ]
        with patch("agents.calendar.agent._load_credentials", return_value=MagicMock()), \
             patch("agents.calendar.agent._list_events", return_value=fake_events):
            ctx = agent.fetch_context("dev")

        assert ctx["api_reachable"] is True
        assert len(ctx["calendar_events"]) == 1
        assert "Good event" in ctx["calendar_events"][0]

    def test_all_day_event_included(self, agent: CalendarAgent):
        """All-day events use date (not dateTime) and are included."""
        fake_events = [
            {
                "summary": "Company offsite",
                "start": {"date": "2026-04-16"},
                "end": {"date": "2026-04-17"},
                "status": "confirmed",
            },
        ]
        with patch("agents.calendar.agent._load_credentials", return_value=MagicMock()), \
             patch("agents.calendar.agent._list_events", return_value=fake_events):
            ctx = agent.fetch_context("dev")

        assert ctx["api_reachable"] is True
        assert len(ctx["calendar_events"]) == 1
        assert "Company offsite" in ctx["calendar_events"][0]

    def test_max_20_events(self, agent: CalendarAgent):
        """Context caps at 20 events even if API returns more."""
        fake_events = [
            {
                "summary": f"Event {i}",
                "start": {"dateTime": f"2026-04-16T{8 + i % 12:02d}:00:00+08:00"},
                "end": {"dateTime": f"2026-04-16T{8 + i % 12:02d}:30:00+08:00"},
                "status": "confirmed",
            }
            for i in range(25)
        ]
        with patch("agents.calendar.agent._load_credentials", return_value=MagicMock()), \
             patch("agents.calendar.agent._list_events", return_value=fake_events):
            ctx = agent.fetch_context("dev")

        assert len(ctx["calendar_events"]) <= 20
        assert len(ctx["calendar_events_rich"]) <= 20

    def test_missing_optional_fields(self, agent: CalendarAgent):
        """Events without attendees, conferenceData, organizer, recurringEventId."""
        fake_events = [
            {
                "summary": "Solo focus time",
                "start": {"dateTime": "2026-04-16T14:00:00+08:00"},
                "end": {"dateTime": "2026-04-16T15:00:00+08:00"},
                "status": "confirmed",
            },
        ]
        with patch("agents.calendar.agent._load_credentials", return_value=MagicMock()), \
             patch("agents.calendar.agent._list_events", return_value=fake_events):
            ctx = agent.fetch_context("dev")

        rich = ctx["calendar_events_rich"]
        assert rich[0]["attendee_count"] == 0
        assert rich[0]["is_recurring"] is False
        assert rich[0]["has_video_call"] is False
        assert rich[0]["organizer"] == ""
        assert rich[0]["duration_min"] == 60


# ── pitch tests ──────────────────────────────────────────────────────


class TestPitch:
    """Tests for CalendarAgent.pitch()."""

    @pytest.fixture
    def brief(self) -> Brief:
        return Brief(
            today_context=TodayContext(
                date="2026-04-16",
                day_of_week="Thursday",
                time_of_day="morning",
                weather_summary="partly cloudy, 18°C",
                calendar_events=["Team standup 10:00"],
            )
        )

    def test_api_unreachable_pitch(self, agent: CalendarAgent, brief: Brief):
        """api_reachable=False produces fallback pitch at priority 0.5."""
        ctx = {"api_reachable": False, "calendar_events": [], "calendar_events_rich": []}
        pitches = agent.pitch(brief, bootstrap_memory(), ctx, "dev")

        assert len(pitches) == 1
        p = pitches[0]
        assert p["priority"] == 0.5
        assert p["claim_kind"] == "neutral"
        assert p["thin_signal"] is False
        assert p["data"]["api_reachable"] is False
        assert p["data"]["events"] == []

    def test_no_events_pitch(self, agent: CalendarAgent, brief: Brief):
        """Empty calendar produces pitch at priority 0.5 with empty events list."""
        ctx = {"api_reachable": True, "calendar_events": [], "calendar_events_rich": []}
        pitches = agent.pitch(brief, bootstrap_memory(), ctx, "dev")

        assert len(pitches) == 1
        p = pitches[0]
        assert p["priority"] == 0.5
        assert p["data"]["api_reachable"] is True
        assert p["data"]["events"] == []

    @pytest.mark.parametrize(
        "event_count,expected_priority",
        [
            (1, 0.55),
            (3, 0.55),
            (4, 0.60),
            (6, 0.60),
            (7, 0.65),
            (10, 0.65),
        ],
    )
    def test_priority_by_event_count(
        self, agent: CalendarAgent, brief: Brief, event_count: int, expected_priority: float
    ):
        """Priority scales with event count."""
        rich = [
            {
                "summary": f"Event {i}",
                "start": f"{9 + i}:00",
                "end": f"{9 + i}:30",
                "duration_min": 30,
                "attendee_count": 2,
                "is_recurring": False,
                "has_video_call": False,
                "organizer": "",
            }
            for i in range(event_count)
        ]
        ctx = {"api_reachable": True, "calendar_events": [], "calendar_events_rich": rich}
        pitches = agent.pitch(brief, bootstrap_memory(), ctx, "dev")

        assert len(pitches) == 1
        assert pitches[0]["priority"] == expected_priority

    def test_pitch_always_neutral_claim(self, agent: CalendarAgent, brief: Brief):
        """Calendar pitch always has claim_kind='neutral'."""
        rich = [{"summary": "Standup", "start": "10:00", "end": "10:30",
                 "duration_min": 30, "attendee_count": 2, "is_recurring": True,
                 "has_video_call": True, "organizer": "Alex"}]
        ctx = {"api_reachable": True, "calendar_events": [], "calendar_events_rich": rich}
        pitches = agent.pitch(brief, bootstrap_memory(), ctx, "dev")

        assert pitches[0]["claim_kind"] == "neutral"
        assert pitches[0]["provenance_shape"] == "balanced"
        assert pitches[0]["thin_signal"] is False

    def test_pitch_data_contains_rich_events(self, agent: CalendarAgent, brief: Brief):
        """Pitch data carries the full rich events list for the Producer LLM."""
        rich = [{"summary": "1:1 with manager", "start": "14:00", "end": "14:30",
                 "duration_min": 30, "attendee_count": 1, "is_recurring": True,
                 "has_video_call": False, "organizer": ""}]
        ctx = {"api_reachable": True, "calendar_events": [], "calendar_events_rich": rich}
        pitches = agent.pitch(brief, bootstrap_memory(), ctx, "dev")

        data = pitches[0]["data"]
        assert data["api_reachable"] is True
        assert data["events"] == rich

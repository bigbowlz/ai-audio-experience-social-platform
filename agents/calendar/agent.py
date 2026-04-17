"""CalendarAgent: today's Google Calendar events via OAuth 2.0.

v0 (demo): Live Google Calendar API, deterministic template pitch.
Token lifecycle: scripts/calendar_auth.py for setup, auto-refresh at runtime.

Spec: agents/calendar/docs/DESIGN.md
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.protocol import (
    AgentMemory,
    Brief,
    Pitch,
    ScopeContext,
    bootstrap_memory,
)

log = logging.getLogger(__name__)

TOKEN_PATH = Path.home() / ".config" / "radio-podcast" / "calendar_token.json"
MAX_EVENTS = 20


# ── Google Calendar API wrapper (mock boundary) ─────────────────────


def _list_events(credentials: Any) -> list[dict] | None:
    """Fetch today's events from Google Calendar API.

    Returns list of event dicts on success, None on auth/network failure.
    This is the single mock boundary for tests.
    """
    from googleapiclient.discovery import build

    try:
        service = build("calendar", "v3", credentials=credentials)

        now = datetime.now(timezone.utc)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)

        result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=start_of_day.isoformat(),
                timeMax=end_of_day.isoformat(),
                maxResults=MAX_EVENTS,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )

        events = result.get("items", [])
        return [e for e in events if e.get("status") != "cancelled"]
    except Exception:
        log.exception("Google Calendar API call failed")
        return None


# ── Credential loading ───────────────────────────────────────────────


def _load_credentials() -> Any | None:
    """Load and refresh OAuth credentials from TOKEN_PATH.

    Returns Credentials object or None if token is missing/revoked.
    """
    if not TOKEN_PATH.exists():
        log.warning("Calendar token not found at %s", TOKEN_PATH)
        return None

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        data = json.loads(TOKEN_PATH.read_text())
        creds = Credentials(
            token=data.get("token"),
            refresh_token=data.get("refresh_token"),
            token_uri=data.get("token_uri"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=data.get("scopes"),
        )

        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Persist refreshed token
            data["token"] = creds.token
            TOKEN_PATH.write_text(json.dumps(data))

        return creds
    except Exception:
        log.exception("Failed to load/refresh calendar credentials")
        return None


# ── DateTime normalization ───────────────────────────────────────────


def _parse_time(dt_str: str) -> str:
    """Parse RFC3339 datetime to HH:MM string."""
    dt = datetime.fromisoformat(dt_str)
    return dt.strftime("%H:%M")


def _parse_duration_min(start_str: str, end_str: str) -> int:
    """Compute duration in minutes between two RFC3339 datetimes."""
    start = datetime.fromisoformat(start_str)
    end = datetime.fromisoformat(end_str)
    return max(0, int((end - start).total_seconds() / 60))


# ── Event normalization ──────────────────────────────────────────────


def _normalize_event(event: dict) -> tuple[str, dict] | None:
    """Normalize a Google Calendar event to (flat_string, rich_dict).

    Returns None if the event has an unparseable datetime.
    """
    summary = event.get("summary", "(No title)")
    start_raw = event.get("start", {})
    end_raw = event.get("end", {})

    # All-day events use "date", timed events use "dateTime"
    is_all_day = "date" in start_raw and "dateTime" not in start_raw

    try:
        if is_all_day:
            flat = f"{summary} (all day)"
            rich = {
                "summary": summary,
                "start": "all-day",
                "end": "all-day",
                "duration_min": 0,
                "attendee_count": len(event.get("attendees", [])),
                "is_recurring": "recurringEventId" in event,
                "has_video_call": "conferenceData" in event,
                "organizer": event.get("organizer", {}).get("displayName", ""),
            }
        else:
            start_dt = start_raw["dateTime"]
            end_dt = end_raw["dateTime"]
            start_time = _parse_time(start_dt)
            end_time = _parse_time(end_dt)
            duration = _parse_duration_min(start_dt, end_dt)

            flat = f"{summary} {start_time}"
            rich = {
                "summary": summary,
                "start": start_time,
                "end": end_time,
                "duration_min": duration,
                "attendee_count": len(event.get("attendees", [])),
                "is_recurring": "recurringEventId" in event,
                "has_video_call": "conferenceData" in event,
                "organizer": event.get("organizer", {}).get("displayName", ""),
            }
    except (KeyError, ValueError, TypeError):
        log.warning("Skipping event with unparseable datetime: %s", summary)
        return None

    return flat, rich


# ── Agent ────────────────────────────────────────────────────────────


class CalendarAgent:
    """Internal DataAgent that supplies today's calendar events."""

    name: str = "calendar"
    display_name: str = "@Calendar"
    scope: str = "Today's Google Calendar events"
    external: bool = False
    price_usdc: float | None = None
    wallet_address: str | None = None

    def load_memory(self, user_id: str) -> AgentMemory:
        return bootstrap_memory()

    def fetch_context(self, user_id: str) -> ScopeContext:
        """Fetch today's events from Google Calendar API.

        Returns ScopeContext with:
          - api_reachable: bool
          - calendar_events: list[str]       (flat strings for Brief)
          - calendar_events_rich: list[dict]  (rich dicts for pitch templates)
        """
        creds = _load_credentials()
        if creds is None:
            return {  # type: ignore[return-value]
                "api_reachable": False,
                "calendar_events": [],
                "calendar_events_rich": [],
            }

        raw_events = _list_events(creds)
        if raw_events is None:
            return {  # type: ignore[return-value]
                "api_reachable": False,
                "calendar_events": [],
                "calendar_events_rich": [],
            }

        flat_events: list[str] = []
        rich_events: list[dict] = []

        for event in raw_events[:MAX_EVENTS]:
            result = _normalize_event(event)
            if result is not None:
                flat, rich = result
                flat_events.append(flat)
                rich_events.append(rich)

        return {  # type: ignore[return-value]
            "api_reachable": True,
            "calendar_events": flat_events,
            "calendar_events_rich": rich_events,
        }

    def pitch(
        self,
        brief: Brief,
        memory: AgentMemory,
        context: ScopeContext,
        user_id: str,
    ) -> list[Pitch]:
        """Emit 1 schedule-context pitch with template hooks.

        Priority scales by event count:
          0 events  -> 0.50
          1-3       -> 0.55
          4-6       -> 0.60
          7+        -> 0.65
        """
        api_reachable = context.get("api_reachable", True)  # type: ignore[call-overload]
        rich_events: list[dict] = context.get("calendar_events_rich") or []  # type: ignore[call-overload]

        if not api_reachable:
            return [
                Pitch(
                    agent="calendar",
                    title="Calendar unavailable",
                    hook="Couldn't reach your calendar today.",
                    rationale="Google Calendar API unreachable.",
                    source_refs=[],
                    priority=0.5,
                    thin_signal=False,
                    claim_kind="neutral",
                    provenance_shape="balanced",
                )
            ]

        n = len(rich_events)

        if n == 0:
            return [
                Pitch(
                    agent="calendar",
                    title="Open day",
                    hook="Your calendar is clear today — wide open from morning to night.",
                    rationale="Calendar: 0 events today.",
                    source_refs=[],
                    priority=0.5,
                    thin_signal=False,
                    claim_kind="neutral",
                    provenance_shape="balanced",
                )
            ]

        hook = _build_hook(rich_events)
        priority = _priority_for_count(n)

        return [
            Pitch(
                agent="calendar",
                title="Today's schedule",
                hook=hook,
                rationale=f"Calendar: {n} event(s) today.",
                source_refs=[],
                priority=priority,
                thin_signal=False,
                claim_kind="neutral",
                provenance_shape="balanced",
            )
        ]


# ── Template hook builder ────────────────────────────────────────────


def _priority_for_count(n: int) -> float:
    """Map event count to priority."""
    if n <= 3:
        return 0.55
    if n <= 6:
        return 0.60
    return 0.65


def _build_hook(events: list[dict]) -> str:
    """Build a conversational template hook from rich event data."""
    n = len(events)

    # Count meetings with video calls
    video_count = sum(1 for e in events if e.get("has_video_call"))

    # Find back-to-back clusters (gap < 15 min)
    back_to_back = 0
    for i in range(len(events) - 1):
        cur_end = events[i].get("end", "")
        next_start = events[i + 1].get("start", "")
        if cur_end and next_start and cur_end != "all-day" and next_start != "all-day":
            try:
                end_h, end_m = map(int, cur_end.split(":"))
                start_h, start_m = map(int, next_start.split(":"))
                gap = (start_h * 60 + start_m) - (end_h * 60 + end_m)
                if gap < 15:
                    back_to_back += 1
            except (ValueError, TypeError):
                pass

    # Find the last event's end time for "free after X" hooks
    last_end = None
    for e in reversed(events):
        if e.get("end") and e["end"] != "all-day":
            last_end = e["end"]
            break

    first = events[0]
    first_label = first["summary"]
    if first["start"] != "all-day":
        first_label += f" at {first['start']}"

    parts = []

    if n == 1:
        parts.append(f"Just one thing on the calendar today — {first_label}.")
    elif back_to_back >= 2:
        parts.append(f"Back-to-back morning with {n} meetings, starting with {first_label}.")
    elif n <= 3:
        parts.append(f"You've got {first_label} and {n - 1} more on the schedule today.")
    else:
        parts.append(f"Busy day ahead — {n} events starting with {first_label}.")

    if video_count >= 3:
        parts.append(f"{video_count} of those are video calls.")
    elif video_count == 1:
        parts.append("One video call in the mix.")

    if last_end and n >= 3:
        parts.append(f"You're free after {last_end}.")

    return " ".join(parts)

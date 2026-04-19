"""CalendarAgent: today's Google Calendar events via OAuth 2.0.

v0 (demo): Live Google Calendar API. Pitch carries raw rich event data
for the Producer LLM — no taste logic here.
Token lifecycle: auth/calendar_auth.py for setup, auto-refresh at runtime.

Spec: agents/calendar/docs/DESIGN.md
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
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
    """Fetch events in the next 16 hours from Google Calendar API.

    Returns list of event dicts on success, None on auth/network failure.
    This is the single mock boundary for tests.
    """
    from googleapiclient.discovery import build

    try:
        service = build("calendar", "v3", credentials=credentials)

        now = datetime.now(timezone.utc)
        window_end = now + timedelta(hours=16)

        result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=window_end.isoformat(),
                maxResults=MAX_EVENTS,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )

        events = result.get("items", [])
        return [e for e in events if e.get("status") != "cancelled"]
    except Exception as e:
        log.warning("Google Calendar API call failed: %s", e)
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
        expiry_str = data.get("expiry")
        creds = Credentials(
            token=data.get("token"),
            refresh_token=data.get("refresh_token"),
            token_uri=data.get("token_uri"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=data.get("scopes"),
            expiry=datetime.fromisoformat(expiry_str) if expiry_str else None,
        )

        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            data["token"] = creds.token
            data["expiry"] = creds.expiry.isoformat() if creds.expiry else None
            TOKEN_PATH.write_text(json.dumps(data))

        return creds
    except Exception as e:
        log.warning("Failed to load/refresh calendar credentials: %s", e)
        return None


# ── DateTime normalization ───────────────────────────────────────────


def _parse_time(dt_str: str) -> str:
    """Parse RFC3339 datetime to 24-hour time string (e.g. '10:00', '14:30')."""
    dt = datetime.fromisoformat(dt_str)
    return f"{dt.hour:02d}:{dt.minute:02d}"


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
        """Emit 1 schedule-context pitch carrying raw rich event data.

        Hook is a structured what/source/goal brief to the Producer — never
        spoken verbatim. Content lives in `data.events`; hook orients the Producer.

        Priority scales by event count:
          0 events  -> 0.50
          1-3       -> 0.55
          4-6       -> 0.60
          7+        -> 0.65
        """
        api_reachable: bool = context.get("api_reachable", True)  # type: ignore[call-overload]
        rich_events: list[dict] = context.get("calendar_events_rich") or []  # type: ignore[call-overload]
        n = len(rich_events)

        if not api_reachable:
            priority = 0.5
        elif n == 0:
            priority = 0.5
        elif n <= 3:
            priority = 0.55
        elif n <= 6:
            priority = 0.60
        else:
            priority = 0.65

        if not api_reachable:
            hook = (
                "WHAT: Schedule segment, degraded (Google Calendar unreachable).\n"
                "SOURCE: Google Calendar (live OAuth, listener's primary calendar) — API call failed.\n"
                "GOAL: Skip or acknowledge briefly; no event content to narrate."
            )
        elif n == 0:
            hook = (
                "WHAT: Schedule segment — 0 upcoming events in the next 16 hours.\n"
                "SOURCE: Google Calendar (live OAuth, listener's primary calendar).\n"
                "GOAL: Frame as an open day; no specific event to reference."
            )
        else:
            hook = (
                f"WHAT: Schedule segment — {n} upcoming event(s) in the next 16 hours.\n"
                "SOURCE: Google Calendar (live OAuth, listener's primary calendar).\n"
                "GOAL: Orient the listener to today's shape before taste segments. "
                "Use data.events as the content source; pick the single most narratively useful event to reference."
            )

        return [
            Pitch(
                agent="calendar",
                title="Today's schedule",
                hook=hook,
                data={"api_reachable": api_reachable, "events": rich_events},
                source_refs=[],
                priority=priority,
                thin_signal=False,
                claim_kind="neutral",
                provenance_shape="balanced",
            )
        ]

"""CalendarAgent: DataAgent stub backed by hardcoded calendar events.

v0: returns hardcoded events so the orchestrator has a concrete value
    to assemble into Brief.today_context.
v1+: replace fetch_context() body with a Google Calendar Takeout or
     Calendar API call.

Spec: agents/docs/DESIGN.md §Interface contract
      agents/docs/prompt_design.md §3 — today_context population
"""

from __future__ import annotations

from agents.protocol import (
    AgentMemory,
    Brief,
    Pitch,
    ScopeContext,
    bootstrap_memory,
)


class CalendarAgent:
    """Internal DataAgent that supplies today's calendar events."""

    name: str = "calendar"
    display_name: str = "@Calendar"
    scope: str = "Today's Google Calendar events"
    external: bool = False
    price_usdc: float | None = None
    wallet_address: str | None = None

    def load_memory(self, user_id: str) -> AgentMemory:
        # Calendar agent has no per-user memory in v0.
        return bootstrap_memory()

    def fetch_context(self, user_id: str) -> ScopeContext:
        """Return today's calendar events.

        v0: hardcoded stub. The orchestrator reads context["calendar_events"]
        and assembles it into Brief.today_context.calendar_events.

        v1+: read from Google Takeout calendar JSON or Google Calendar API.
        """
        return {                                        # type: ignore[return-value]
            "calendar_events": [
                "Team standup 10am",
                "Lunch with Alex 12:30pm",
            ]
        }

    def pitch(
        self,
        brief: Brief,
        memory: AgentMemory,
        context: ScopeContext,
        user_id: str,
    ) -> list[Pitch]:
        """Emit 1 schedule-context pitch.

        Calendar agent always emits exactly 1 pitch. thin_signal=False because
        the data is always present in v0 (even an empty calendar is a valid
        "nothing on today" pitch).
        """
        events: list[str] = context.get("calendar_events") or []  # type: ignore[call-overload]

        if not events:
            hook = "Your calendar is clear today — a good day to let the music lead."
            title = "Open day"
        else:
            first = events[0]
            rest = len(events) - 1
            suffix = f" and {rest} more event{'s' if rest > 1 else ''}" if rest else ""
            hook = (
                f"You've got {first}{suffix} on the schedule today. "
                f"Here's a listening plan that fits around it."
            )
            title = "Today's schedule"

        return [
            Pitch(
                agent="calendar",
                title=title,
                hook=hook,
                suggested_length_sec=30,
                rationale=f"Calendar: {len(events)} event(s) today.",
                source_refs=[],
                priority=0.6,
                thin_signal=False,
                claim_kind="neutral",
                provenance_shape="balanced",
            )
        ]

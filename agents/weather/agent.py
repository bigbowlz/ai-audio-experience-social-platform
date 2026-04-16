"""WeatherAgent: DataAgent stub backed by hardcoded weather data.

v0: returns a hardcoded weather summary so the orchestrator has a
    concrete value to assemble into Brief.today_context.
v1+: replace fetch_context() body with a live Open-Meteo API call
     (free, no key required).

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


class WeatherAgent:
    """Internal DataAgent that supplies today's weather summary."""

    name: str = "weather"
    display_name: str = "@Weather"
    scope: str = "Current weather conditions via Open-Meteo"
    external: bool = False
    price_usdc: float | None = None
    wallet_address: str | None = None

    def load_memory(self, user_id: str) -> AgentMemory:
        # Weather agent has no per-user memory in v0.
        return bootstrap_memory()

    def fetch_context(self, user_id: str) -> ScopeContext:
        """Return today's weather summary.

        v0: hardcoded stub. The orchestrator reads context["weather_summary"]
        and assembles it into Brief.today_context.weather_summary.

        v1+: call Open-Meteo /v1/forecast with user's lat/lon from their profile.
        """
        return {"weather_summary": "partly cloudy, 18°C"}  # type: ignore[return-value]

    def pitch(
        self,
        brief: Brief,
        memory: AgentMemory,
        context: ScopeContext,
        user_id: str,
    ) -> list[Pitch]:
        """Emit 1 weather-context pitch.

        Weather agent always emits exactly 1 pitch — it has 1 subject (today's
        weather) and no topic-ranked alternatives. thin_signal=False because
        the data is always present in v0.
        """
        weather = context.get("weather_summary") or "clear skies"  # type: ignore[call-overload]
        today = brief["today_context"]
        time = today.get("time_of_day", "morning")

        hook = (
            f"It's {weather} out there on this {time}. "
            f"Here's how today's conditions set the mood for your listening."
        )

        return [
            Pitch(
                agent="weather",
                title="Today's weather",
                hook=hook,
                rationale=f"Weather context: {weather}",
                source_refs=[],
                priority=0.5,
                thin_signal=False,
                claim_kind="neutral",
                provenance_shape="balanced",
            )
        ]

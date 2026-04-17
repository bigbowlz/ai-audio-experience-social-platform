"""WeatherAgent: DataAgent backed by live Open-Meteo API.

Fetches current conditions, hourly forecast, air quality from Open-Meteo
(free, no API key). Produces a deterministic narrative summary and exactly
1 pitch per episode.

Spec: agents/weather/docs/DESIGN.md
      agents/docs/DESIGN.md §Interface contract
      agents/docs/prompt_design.md §3 — today_context population
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from agents.protocol import (
    AgentMemory,
    Brief,
    Pitch,
    ScopeContext,
    bootstrap_memory,
)


# ── WMO weather code mapping ──

WMO_CONDITIONS: dict[int, str] = {
    0: "clear",
    1: "mostly_clear", 2: "partly_cloudy", 3: "overcast",
    45: "fog", 48: "freezing_fog",
    51: "light_drizzle", 53: "drizzle", 55: "heavy_drizzle",
    61: "light_rain", 63: "rain", 65: "heavy_rain",
    71: "light_snow", 73: "snow", 75: "heavy_snow",
    80: "light_showers", 81: "showers", 82: "heavy_showers",
    95: "thunderstorm", 96: "thunderstorm_hail", 99: "heavy_thunderstorm_hail",
}


def wmo_condition(code: int) -> str:
    """Map a WMO weather code to a human-readable condition string."""
    return WMO_CONDITIONS.get(code, "unknown")


# ── Wind direction ──

_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def wind_direction(degrees: float) -> str:
    """Convert wind direction in degrees to 8-point compass."""
    return _COMPASS[round(degrees / 45) % 8]


# ── AQI category ──

def aqi_category(aqi: int) -> str:
    """European AQI to category string."""
    if aqi <= 20:
        return "good"
    if aqi <= 40:
        return "fair"
    if aqi <= 60:
        return "moderate"
    if aqi <= 80:
        return "poor"
    return "very_poor"


# ── Notable facts ──

_CATEGORY_ORDER = ["precipitation", "uv", "wind", "temperature", "air_quality", "visibility"]
_SEVERITY_RANK = {"warning": 0, "notable": 1, "info": 2}


def notable_facts(weather_data: dict) -> list[dict]:
    """Extract top 3 most radio-interesting weather facts.

    Deterministic: sort by severity > specificity > canonical category order.
    """
    facts: list[dict] = []
    hourly = weather_data.get("hourly_forecast", [])
    daily = weather_data.get("daily", {})
    current = weather_data.get("current", {})
    aq = weather_data.get("air_quality")

    # UV index: peak across hourly forecast
    max_uv = 0.0
    max_uv_hour: int | None = None
    for h in hourly:
        if h["uv_index"] > max_uv:
            max_uv = h["uv_index"]
            max_uv_hour = h["hour"]
    if max_uv >= 8:
        facts.append({
            "category": "uv",
            "summary": f"UV index peaks at {max_uv:.0f} (very high) around {max_uv_hour}:00",
            "severity": "warning",
            "hour": max_uv_hour,
        })
    elif max_uv >= 6:
        facts.append({
            "category": "uv",
            "summary": f"UV index reaches {max_uv:.0f} (high) around {max_uv_hour}:00",
            "severity": "notable",
            "hour": max_uv_hour,
        })

    # Precipitation probability: first hour > 60%
    for h in hourly:
        if h["precipitation_probability"] > 60:
            prob = h["precipitation_probability"]
            hour = h["hour"]
            facts.append({
                "category": "precipitation",
                "summary": f"Rain likely around {hour}:00 with {prob}% chance",
                "severity": "notable",
                "hour": hour,
            })
            break

    # Wind: peak > 40 km/h
    max_wind = 0.0
    max_wind_hour: int | None = None
    for h in hourly:
        if h["wind_speed_kmh"] > max_wind:
            max_wind = h["wind_speed_kmh"]
            max_wind_hour = h["hour"]
    if max_wind > 40:
        facts.append({
            "category": "wind",
            "summary": f"Wind gusts up to {max_wind:.0f} km/h around {max_wind_hour}:00",
            "severity": "notable",
            "hour": max_wind_hour,
        })

    # Temperature swing > 15F
    high = daily.get("high_f", 0)
    low = daily.get("low_f", 0)
    if high - low > 15:
        facts.append({
            "category": "temperature",
            "summary": f"Temperature swings {high - low:.0f}F today, from {low:.0f}F to {high:.0f}F",
            "severity": "notable",
            "hour": None,
        })

    # Feels-like divergence > 9F: current OR max divergence in next 6h
    feels_diff = abs(current.get("temperature_f", 0) - current.get("feels_like_f", 0))
    max_feels_hour: int | None = None
    for h in hourly[:6]:
        diff = abs(h["temperature_f"] - h["feels_like_f"])
        if diff > feels_diff:
            feels_diff = diff
            max_feels_hour = h["hour"]
    if feels_diff > 9:
        if max_feels_hour is not None:
            # Worst divergence is in the forecast window
            h_entry = next(h for h in hourly[:6] if h["hour"] == max_feels_hour)
            actual = h_entry["temperature_f"]
            feels = h_entry["feels_like_f"]
        else:
            actual = current.get("temperature_f", 0)
            feels = current.get("feels_like_f", 0)
        facts.append({
            "category": "temperature",
            "summary": f"Feels like {feels:.0f}F despite being {actual:.0f}F",
            "severity": "notable",
            "hour": max_feels_hour,
        })

    # Air quality
    if aq is not None:
        aqi_val = aq.get("aqi", 0)
        if aqi_val > 100:
            facts.append({
                "category": "air_quality",
                "summary": f"Air quality is {aqi_category(aqi_val).replace('_', ' ')} with AQI {aqi_val}",
                "severity": "warning",
                "hour": None,
            })
        elif aqi_val > 50:
            facts.append({
                "category": "air_quality",
                "summary": f"Air quality is {aqi_category(aqi_val).replace('_', ' ')} with AQI {aqi_val}",
                "severity": "notable",
                "hour": None,
            })

    # Visibility < 5 km (current or any hourly)
    low_vis = False
    if current.get("visibility_km", 10) < 5:
        low_vis = True
    if not low_vis:
        for h in hourly:
            if h.get("visibility_km", 10) < 5:
                low_vis = True
                break
    if low_vis:
        facts.append({
            "category": "visibility",
            "summary": "Reduced visibility below 5 km expected",
            "severity": "notable",
            "hour": None,
        })

    # Deterministic sort: severity (warning > notable > info),
    # specificity (has hour > no hour), canonical category order.
    facts.sort(key=lambda f: (
        _SEVERITY_RANK.get(f["severity"], 9),
        0 if f["hour"] is not None else 1,
        _CATEGORY_ORDER.index(f["category"]) if f["category"] in _CATEGORY_ORDER else 99,
    ))

    return facts[:3]


# ── Narrative compiler ──

def narrative_compiler(weather_data: dict, facts: list[dict] | None = None) -> str:
    """Build a 2-3 sentence deterministic narrative from weather data.

    Always starts with current conditions. 2 sentences for 0-1 notable facts,
    up to 3 for 2+ notable facts. Max ~60 words.

    Pass pre-computed facts to avoid recomputing them.
    """
    current = weather_data.get("current", {})
    temp = current.get("temperature_f", 0)
    condition = current.get("condition", "unknown").replace("_", " ")

    if facts is None:
        facts = notable_facts(weather_data)

    parts = [f"Currently {temp:.0f}F and {condition}."]

    if len(facts) == 0:
        daily = weather_data.get("daily", {})
        high = daily.get("high_f")
        low = daily.get("low_f")
        if high is not None and low is not None:
            parts.append(f"Highs near {high:.0f}F, lows around {low:.0f}F.")
        else:
            parts.append("Conditions look steady.")
    elif len(facts) == 1:
        parts.append(facts[0]["summary"] + ".")
    else:
        parts.append(facts[0]["summary"] + ".")
        parts.append(facts[1]["summary"] + ".")

    return " ".join(parts)


# ── Location helper ──

_LOCATION_PATH = Path.home() / ".config" / "radio-podcast" / "weather_location.json"


def _get_user_location(user_id: str) -> tuple[float, float, str] | None:
    """Read lat/lon/name from auth/weather.py saved JSON. Returns None if not set."""
    if not _LOCATION_PATH.exists():
        return None
    try:
        data = json.loads(_LOCATION_PATH.read_text())
        return (data["lat"], data["lon"], data["location_name"])
    except (KeyError, json.JSONDecodeError):
        return None


def _current_hour() -> int:
    """Return the current local hour (0-23). Extracted for testability."""
    return datetime.now().hour


# ── Response parsing ──

def _parse_forecast(resp: dict, current_hour: int) -> dict:
    """Parse Open-Meteo /v1/forecast response into typed weather data."""
    raw_current = resp["current"]
    raw_hourly = resp["hourly"]
    raw_daily = resp["daily"]

    # Current conditions
    current = {
        "temperature_f": raw_current["temperature_2m"],
        "feels_like_f": raw_current["apparent_temperature"],
        "condition": wmo_condition(raw_current["weather_code"]),
        "wind_speed_kmh": raw_current["wind_speed_10m"],
        "wind_direction": wind_direction(raw_current["wind_direction_10m"]),
        "humidity": raw_current["relative_humidity_2m"],
        "visibility_km": raw_current["visibility"] / 1000,
        # UV not in current block; source from hourly
        "uv_index": raw_hourly["uv_index"][current_hour] if current_hour < len(raw_hourly["uv_index"]) else 0.0,
    }

    # Hourly forecast: next 24h from current hour
    n_hours = len(raw_hourly["time"])
    hourly_forecast = []
    for i in range(current_hour, min(current_hour + 24, n_hours)):
        hourly_forecast.append({
            "hour": i % 24,
            "temperature_f": raw_hourly["temperature_2m"][i],
            "feels_like_f": raw_hourly["apparent_temperature"][i],
            "condition": wmo_condition(raw_hourly["weather_code"][i]),
            "precipitation_probability": raw_hourly["precipitation_probability"][i],
            "precipitation_mm": raw_hourly["precipitation"][i],
            "wind_speed_kmh": raw_hourly["wind_speed_10m"][i],
            "wind_direction": wind_direction(raw_hourly["wind_direction_10m"][i]),
            "visibility_km": raw_hourly["visibility"][i] / 1000,
            "uv_index": raw_hourly["uv_index"][i],
            "humidity": raw_hourly["relative_humidity_2m"][i],
        })

    # Daily summary
    daily = {
        "high_f": raw_daily["temperature_2m_max"][0],
        "low_f": raw_daily["temperature_2m_min"][0],
        "sunrise": raw_daily["sunrise"][0].split("T")[1] if "T" in raw_daily["sunrise"][0] else raw_daily["sunrise"][0],
        "sunset": raw_daily["sunset"][0].split("T")[1] if "T" in raw_daily["sunset"][0] else raw_daily["sunset"][0],
        "max_uv": raw_daily["uv_index_max"][0],
        "total_precipitation_mm": raw_daily["precipitation_sum"][0],
        "dominant_condition": wmo_condition(raw_daily["weather_code"][0]),
    }

    return {"current": current, "hourly_forecast": hourly_forecast, "daily": daily}


def _parse_air_quality(resp: dict) -> dict:
    """Parse Open-Meteo /v1/air-quality response."""
    raw = resp["current"]
    aqi_val = raw["european_aqi"]
    return {
        "aqi": aqi_val,
        "pm25": raw["pm2_5"],
        "pm10": raw["pm10"],
        "category": aqi_category(aqi_val),
    }


# ── Open-Meteo API URLs ──

_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
_HTTP_TIMEOUT = 3.0


class WeatherAgent:
    """Internal DataAgent that supplies today's weather summary."""

    name: str = "weather"
    display_name: str = "@Weather"
    scope: str = "Current weather conditions via Open-Meteo"
    external: bool = False
    price_usdc: float | None = None
    wallet_address: str | None = None

    def load_memory(self, user_id: str) -> AgentMemory:
        return bootstrap_memory()

    def fetch_context(self, user_id: str) -> ScopeContext:
        """Return today's weather data from Open-Meteo.

        The orchestrator reads context["weather_summary"] and assembles it
        into Brief.today_context.weather_summary.
        """
        location = _get_user_location(user_id)
        if location is None:
            return {"weather_summary": None}  # type: ignore[return-value]

        lat, lon, location_name = location
        current_hour = _current_hour()

        forecast_params = {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m,visibility",
            "hourly": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation_probability,precipitation,weather_code,wind_speed_10m,wind_direction_10m,visibility,uv_index",
            "daily": "temperature_2m_max,temperature_2m_min,sunrise,sunset,uv_index_max,precipitation_sum,weather_code",
            "forecast_days": 2,
            "timezone": "auto",
            "temperature_unit": "fahrenheit",
        }

        aq_params = {
            "latitude": lat,
            "longitude": lon,
            "current": "european_aqi,pm2_5,pm10",
            "timezone": "auto",
        }

        weather_data: dict | None = None
        air_quality: dict | None = None

        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            # Forecast (required)
            try:
                resp = client.get(_FORECAST_URL, params=forecast_params)
                resp.raise_for_status()
                weather_data = _parse_forecast(resp.json(), current_hour)
            except (httpx.HTTPError, KeyError):
                return {"weather_summary": None}  # type: ignore[return-value]

            # Air quality (optional)
            try:
                resp = client.get(_AIR_QUALITY_URL, params=aq_params)
                resp.raise_for_status()
                air_quality = _parse_air_quality(resp.json())
            except (httpx.HTTPError, KeyError):
                air_quality = None

        weather_data["air_quality"] = air_quality
        facts = notable_facts(weather_data)
        summary = narrative_compiler(weather_data, facts)

        return {  # type: ignore[return-value]
            "weather_summary": summary,
            "current": weather_data["current"],
            "hourly_forecast": weather_data["hourly_forecast"],
            "daily": weather_data["daily"],
            "air_quality": air_quality,
            "notable_facts": facts,
            "location_name": location_name,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    def pitch(
        self,
        brief: Brief,
        memory: AgentMemory,
        context: ScopeContext,
        user_id: str,
    ) -> list[Pitch]:
        """Emit 1 weather-context pitch. Deterministic, no LLM."""
        summary = context.get("weather_summary")  # type: ignore[call-overload]

        # Degraded: no weather data
        if not summary:
            return [Pitch(
                agent="weather",
                title="Weather",
                hook="Weather data unavailable.",
                rationale="Weather fetch failed or no location set.",
                source_refs=[],
                priority=0.3,
                thin_signal=True,
                claim_kind="neutral",
                provenance_shape="balanced",
                data={},
            )]

        notable = context.get("notable_facts", [])  # type: ignore[call-overload]
        location = context.get("location_name", "your area")  # type: ignore[call-overload]

        if notable:
            highlights = "; ".join(f["summary"] for f in notable[:3])
            hook = f"Weather in {location}: {highlights}."
        else:
            hook = f"Weather in {location}: {summary}"

        return [Pitch(
            agent="weather",
            title=f"Weather in {location}",
            hook=hook,
            rationale=summary,
            source_refs=[],
            priority=0.5,
            thin_signal=False,
            claim_kind="neutral",
            provenance_shape="balanced",
            data={
                "current": context.get("current"),  # type: ignore[call-overload]
                "daily": context.get("daily"),  # type: ignore[call-overload]
                "hourly_forecast": context.get("hourly_forecast"),  # type: ignore[call-overload]
                "air_quality": context.get("air_quality"),  # type: ignore[call-overload]
                "notable_facts": notable,
                "location_name": location,
                "fetched_at": context.get("fetched_at"),  # type: ignore[call-overload]
            },
        )]

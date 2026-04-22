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
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

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


def _time_label(hour: int, first_hour: int) -> str:
    """Render '{hour}:00', appending ' tomorrow' when the hour has wrapped past midnight.

    hourly_forecast is ordered from the current hour forward; any entry whose hour
    is numerically less than the first entry's hour has wrapped past midnight.
    """
    if hour < first_hour:
        return f"{hour}:00 tomorrow"
    return f"{hour}:00"


def notable_facts(weather_data: dict) -> list[dict]:
    """Extract top 3 most radio-interesting weather facts.

    Deterministic: sort by severity > specificity > canonical category order.
    """
    facts: list[dict] = []
    hourly = weather_data.get("hourly_forecast", [])
    day_ahead = weather_data.get("day_ahead", {})
    current = weather_data.get("current", {})
    aq = weather_data.get("air_quality")

    first_hour = hourly[0]["hour"] if hourly else 0
    sunset_past = day_ahead.get("sunset") is None

    # UV index: peak across hourly forecast. Suppress when sun has already set
    # today AND the peak lives in tomorrow's half of the window — a night-time
    # brief surfacing tomorrow's UV reads as stale.
    max_uv = 0.0
    max_uv_hour: int | None = None
    for h in hourly:
        if h["uv_index"] > max_uv:
            max_uv = h["uv_index"]
            max_uv_hour = h["hour"]
    uv_peak_tomorrow = max_uv_hour is not None and max_uv_hour < first_hour
    suppress_uv = sunset_past and uv_peak_tomorrow
    if max_uv >= 8 and not suppress_uv:
        facts.append({
            "category": "uv",
            "summary": f"UV index peaks at {max_uv:.0f} (very high) around {_time_label(max_uv_hour, first_hour)}",
            "severity": "warning",
            "hour": max_uv_hour,
        })
    elif max_uv >= 6 and not suppress_uv:
        facts.append({
            "category": "uv",
            "summary": f"UV index reaches {max_uv:.0f} (high) around {_time_label(max_uv_hour, first_hour)}",
            "severity": "notable",
            "hour": max_uv_hour,
        })

    # Precipitation: qualify on prob > 60%, anchor on peak-mm hour, note the
    # first qualifying hour separately when it differs from the peak — so the
    # summary surfaces the heaviest event and still tells listeners when rain
    # starts. Falls back cleanly when mm data is absent (all zeros → treated
    # as single-hour phrasing, max() returns first-max by Python semantics).
    rain_hours = [h for h in hourly if h["precipitation_probability"] > 60]
    if rain_hours:
        start = rain_hours[0]
        peak = max(rain_hours, key=lambda h: h["precipitation_mm"])
        peak_hour = peak["hour"]
        peak_prob = peak["precipitation_probability"]
        peak_mm = peak["precipitation_mm"]
        start_hour = start["hour"]
        has_meaningful_mm = peak_mm >= 0.1

        peak_label = _time_label(peak_hour, first_hour)
        start_label = _time_label(start_hour, first_hour)
        if peak_hour == start_hour:
            if has_meaningful_mm:
                summary = (
                    f"Rain likely around {peak_label} "
                    f"({peak_mm:.1f} mm, {peak_prob}% chance)"
                )
            else:
                summary = f"Rain likely around {peak_label} with {peak_prob}% chance"
        else:
            if has_meaningful_mm:
                summary = (
                    f"Rain starting around {start_label}, heaviest around "
                    f"{peak_label} ({peak_mm:.1f} mm, {peak_prob}% chance)"
                )
            else:
                summary = (
                    f"Rain starting around {start_label}, heaviest around "
                    f"{peak_label} with {peak_prob}% chance"
                )

        facts.append({
            "category": "precipitation",
            "summary": summary,
            "severity": "notable",
            "hour": peak_hour,
        })

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
            "summary": f"Wind gusts up to {max_wind:.0f} km/h around {_time_label(max_wind_hour, first_hour)}",
            "severity": "notable",
            "hour": max_wind_hour,
        })

    # Temperature swing > 15F across the forward 24h window. Drop the word
    # "today": the window can straddle midnight and "today" then misleads.
    high = day_ahead.get("high_f")
    low = day_ahead.get("low_f")
    if high is not None and low is not None and high - low > 15:
        facts.append({
            "category": "temperature",
            "summary": f"Temperature swings {high - low:.0f}F, from {low:.0f}F to {high:.0f}F",
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
        da = weather_data.get("day_ahead", {})
        high = da.get("high_f")
        low = da.get("low_f")
        hours_left = da.get("hours_remaining", 0)
        if high is not None and low is not None and hours_left >= 2:
            parts.append(f"Expecting highs near {high:.0f}F, lows around {low:.0f}F.")
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

_PEAK_HOUR_THRESHOLD = 15  # heuristic: daily high has typically been reached by 15:00 local


def _hour_from_timestr(t: str) -> int:
    """Parse 'HH:MM' to hour int. Returns 25 (treat-as-past) if empty/unparseable."""
    if not t or ":" not in t:
        return 25
    try:
        return int(t.split(":")[0])
    except ValueError:
        return 25


def _parse_day_blocks(
    daily: dict,
    hourly_forecast: list[dict],
    past_precip_mm: float,
    current_hour: int,
) -> tuple[dict, dict]:
    """Split daily data into day_past (already happened) and day_ahead (remaining).

    daily: already-parsed daily dict with high_f, low_f, sunrise, sunset, max_uv,
           total_precipitation_mm, dominant_condition.
    hourly_forecast: already-parsed next-24h slice.
    past_precip_mm: sum of raw hourly precipitation for hours 0..current_hour.
    current_hour: local hour (0-23), from _current_hour().
    """
    sunrise_str = daily.get("sunrise", "")
    sunset_str = daily.get("sunset", "")
    sunrise_hour = _hour_from_timestr(sunrise_str)
    sunset_hour = _hour_from_timestr(sunset_str)

    day_past = {
        "sunrise": sunrise_str if sunrise_hour <= current_hour else None,
        "high_f": daily.get("high_f") if current_hour >= _PEAK_HOUR_THRESHOLD else None,
        "precipitation_mm_so_far": round(past_precip_mm, 1),
    }

    if hourly_forecast:
        ahead_high = max(h["temperature_f"] for h in hourly_forecast)
        ahead_low = min(h["temperature_f"] for h in hourly_forecast)
    else:
        ahead_high = None
        ahead_low = None

    day_ahead = {
        "high_f": ahead_high,
        "low_f": ahead_low,
        "hours_remaining": len(hourly_forecast),
        "sunset": sunset_str if sunset_hour > current_hour else None,
        "total_precipitation_mm": daily.get("total_precipitation_mm"),
        "dominant_condition": daily.get("dominant_condition"),
        "max_uv": daily.get("max_uv"),
    }

    return day_past, day_ahead


def _parse_forecast(resp: dict, current_hour: int) -> dict:
    """Parse Open-Meteo /v1/forecast response into typed weather data.

    Returns current conditions, 24h hourly forecast, and two time-aware blocks:
    day_past (what already happened today) and day_ahead (what's still coming).
    """
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

    n_hours = len(raw_hourly["time"])

    # Past precipitation: sum hourly precipitation for hours 0..current_hour
    past_precip_mm = sum(raw_hourly["precipitation"][i] for i in range(min(current_hour, n_hours)))

    # Hourly forecast: next 24h from current hour
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

    # Parse daily aggregates into intermediate dict for _parse_day_blocks
    daily = {
        "high_f": raw_daily["temperature_2m_max"][0],
        "low_f": raw_daily["temperature_2m_min"][0],
        "sunrise": raw_daily["sunrise"][0].split("T")[1] if "T" in raw_daily["sunrise"][0] else raw_daily["sunrise"][0],
        "sunset": raw_daily["sunset"][0].split("T")[1] if "T" in raw_daily["sunset"][0] else raw_daily["sunset"][0],
        "max_uv": raw_daily["uv_index_max"][0],
        "total_precipitation_mm": raw_daily["precipitation_sum"][0],
        "dominant_condition": wmo_condition(raw_daily["weather_code"][0]),
    }

    day_past, day_ahead = _parse_day_blocks(daily, hourly_forecast, past_precip_mm, current_hour)

    return {
        "current": current,
        "hourly_forecast": hourly_forecast,
        "day_past": day_past,
        "day_ahead": day_ahead,
    }


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
_HTTP_TIMEOUT = 5.0
_HTTP_RETRIES = 2
# Backoff sleep before retry attempt N (1-indexed): (before attempt 2, before attempt 3).
# Must be length == _HTTP_RETRIES so schedule stays in lockstep with retry count.
_HTTP_RETRY_BACKOFF_S: tuple[float, ...] = (0.5, 1.5)


def _get_json(client: httpx.Client, url: str, params: dict) -> dict | None:
    """GET a JSON endpoint with exponential backoff on httpx.HTTPError.

    Returns parsed JSON on success, or None after all attempts fail. Each
    attempt logs its own warning so intermittent failures leave a trace.
    """
    last_exc: Exception | None = None
    for attempt in range(_HTTP_RETRIES + 1):
        if attempt > 0:
            time.sleep(_HTTP_RETRY_BACKOFF_S[attempt - 1])
        try:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            last_exc = e
            log.warning(
                "Open-Meteo GET %s failed (attempt %d/%d): %s: %s",
                url, attempt + 1, _HTTP_RETRIES + 1, type(e).__name__, e,
            )
    log.warning(
        "Open-Meteo GET %s gave up after %d attempts (last error: %s)",
        url, _HTTP_RETRIES + 1, last_exc,
    )
    return None


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
            forecast_json = _get_json(client, _FORECAST_URL, forecast_params)
            if forecast_json is None:
                return {"weather_summary": None}  # type: ignore[return-value]
            try:
                weather_data = _parse_forecast(forecast_json, current_hour)
            except KeyError as e:
                log.warning("Open-Meteo forecast response shape unexpected: missing %s", e)
                return {"weather_summary": None}  # type: ignore[return-value]

            # Air quality (optional)
            aq_json = _get_json(client, _AIR_QUALITY_URL, aq_params)
            if aq_json is None:
                air_quality = None
            else:
                try:
                    air_quality = _parse_air_quality(aq_json)
                except KeyError as e:
                    log.warning("Open-Meteo air-quality response shape unexpected: missing %s", e)
                    air_quality = None

        weather_data["air_quality"] = air_quality
        facts = notable_facts(weather_data)
        summary = narrative_compiler(weather_data, facts)

        return {  # type: ignore[return-value]
            "weather_summary": summary,
            "current": weather_data["current"],
            "day_past": weather_data["day_past"],
            "day_ahead": weather_data["day_ahead"],
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
        """Emit 1 weather-context pitch. Deterministic, no LLM.

        Hook is a structured what/source/goal brief to the Producer — never
        spoken verbatim. Content lives in `data`; hook orients the Producer.
        """
        summary = context.get("weather_summary")  # type: ignore[call-overload]

        # Degraded: no weather data
        if not summary:
            return [Pitch(
                agent="weather",
                title="Weather",
                hook=(
                    "WHAT: Weather segment, degraded (no data available).\n"
                    "SOURCE: Open-Meteo live feed (listener's GPS location) — fetch failed or no location set.\n"
                    "GOAL: Acknowledge briefly; no forecast content to narrate."
                ),
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
            what = f"Weather for {location} — {highlights}"
        else:
            what = f"Weather for {location} — steady conditions, no notable facts"

        hook = (
            f"WHAT: {what}.\n"
            f"SOURCE: Open-Meteo live feed (listener's GPS location: {location}).\n"
            f"GOAL: Ground the show in today's real-world conditions. "
            f"Use data.current, data.day_ahead, and data.notable_facts as the content source; this hook is orientation only."
        )

        return [Pitch(
            agent="weather",
            title=f"Weather in {location}",
            hook=hook,
            source_refs=[],
            priority=0.5,
            thin_signal=False,
            claim_kind="neutral",
            provenance_shape="balanced",
            data={
                "current": context.get("current"),  # type: ignore[call-overload]
                "day_past": context.get("day_past"),  # type: ignore[call-overload]
                "day_ahead": context.get("day_ahead"),  # type: ignore[call-overload]
                "air_quality": context.get("air_quality"),  # type: ignore[call-overload]
                "notable_facts": notable,
                "location_name": location,
                "fetched_at": context.get("fetched_at"),  # type: ignore[call-overload]
            },
        )]

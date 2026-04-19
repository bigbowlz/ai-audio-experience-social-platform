# Agent: `weather_agent`

**Status:** APPROVED — office-hours design cleared 2026-04-16; updated 2026-04-17 to replace `daily` with time-aware `day_past` + `day_ahead`, and refine notable-fact summaries (peak-mm precipitation event, tomorrow labels for wrapped hours, UV night-time suppression)
**Parent component doc:** [`../../docs/DESIGN.md`](../../docs/DESIGN.md) — `agents` component (shared `DataAgent` protocol, memory shape, `Pitch` shape)
**Session artifact:** `~/.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260416-203021.md`
**Scope:** demo (v0). Live Open-Meteo API + deterministic narrative summary + notable-facts ranking.

## Purpose

Supply real-time weather data to the Brief (context pipe) and emit 1 weather-context pitch to the Producer. The agent owns:

1. **Weather acquisition** — fetch current conditions, hourly forecast, and air quality from Open-Meteo (free, no API key).
2. **Context pipe** — populate `Brief.today_context.weather_summary` with a deterministic narrative string.
3. **Narrative compilation** — deterministic English summary highlighting the top 3 most radio-interesting weather facts. No LLM call.
4. **Pitch generation** — deterministic template hook using notable facts and narrative. No LLM call.

Weather is a context agent, not a topic agent. It always emits exactly 1 pitch with `claim_kind="neutral"`, priority 0.5. It competes for segment time alongside YouTube, calendar, and alices, but its value is grounding the show in the real world — listeners can look outside and verify.

## Architecture

```
fetch_context(user_id)
  |
  +-- Read lat/lon from user profile
  |   No location -> return {"weather_summary": None}
  |
  +-- GET /v1/forecast (timeout 3s)
  +-- GET /v1/air-quality (timeout 3s)
  |   (sequential via httpx.Client, ~200ms each typical)
  |
  +-- Parse responses into WeatherData
  |   Forecast fails -> return {"weather_summary": None}
  |   AQ fails -> proceed without AQ, set air_quality=None
  |
  +-- narrative_compiler(weather_data) -> str (2-3 sentences, ~60 words max)
  +-- notable_facts(weather_data) -> list[WeatherFact] (top 3)
  |
  +-- ScopeContext output: WeatherScopeContext
      {
        "weather_summary": str,                    # narrative for Brief
        "current": CurrentConditions,
        "day_past": DayPast,                       # what already happened today
        "day_ahead": DayAhead,                     # what's still coming today
        "air_quality": AirQuality | None,
        "notable_facts": list[WeatherFact],        # top 3
        "location_name": str,
        "fetched_at": str                          # ISO 8601
      }
      # hourly_forecast is computed internally (drives notable_facts and
      # the narrative compiler) but not surfaced — the Producer consumes
      # notable_facts and day_ahead, not raw hourly data.

pitch(brief, memory, context, user_id)
  |
  +-- weather_summary is None -> "Weather data unavailable." (priority 0.3, thin_signal=True)
  +-- No notable facts        -> Hook from summary + location  (priority 0.5)
  +-- Notable facts exist     -> Hook from top 3 highlights    (priority 0.5)
```

### Key design decisions

| Decision | Choice | Why |
|----------|--------|-----|
| No LLM in agent | Deterministic narrative + template hooks | Producer LLM rewrites hooks into radio script. Two LLM calls for one weather sentence is wasteful. |
| Two Open-Meteo endpoints | `/v1/forecast` + `/v1/air-quality` | AQ is a separate endpoint. Both are free and keyless. |
| Sequential HTTP (not parallel) | `httpx.Client` with 3s timeout per call | Simpler than nested thread pools. Orchestrator already runs `fetch_context()` in a ThreadPoolExecutor. ~400ms total typical, 6s worst case. |
| `weather_summary` stays `str \| None` | Backward compatibility | Orchestrator copies this into `Brief.today_context.weather_summary`. No Brief schema change needed. |
| Notable-facts ranking | Deterministic sort by severity > specificity > category | Consistent output across runs. Radio-interesting facts surface first. |
| No third-party weather libs | Raw `httpx` + Open-Meteo JSON | `python-open-meteo` and `meteocalc` are unnecessary. Open-Meteo returns `apparent_temperature` directly. |

### Data flow through the system

```
                PHASE 1: fetch_context (parallel with other agents)
                ==================================================
  Open-Meteo                        weather_agent
  +--------------+   GET /forecast   +----------------------------+
  | /v1/forecast | ----------------> | parse current, hourly;     |
  +--------------+                   | split into day_past +      |
                                     | day_ahead via current_hour |
                                     +----------------------------+
  +-----------------+  GET /air-q    +----------------------------+
  | /v1/air-quality | ------------> | parse AQI, PM2.5, PM10     |
  +-----------------+               +----------------------------+
                                              |
                                    narrative_compiler() -> str
                                    notable_facts() -> top 3
                                              |
                                    ScopeContext to orchestrator
                                              |
                SYNC BARRIER: all fetch_context() done
                ======================================
                                              |
                            orchestrator assembles Brief:
                            Brief.today_context.weather_summary
                            = context["weather_summary"]
                                              |
                PHASE 2: pitch (parallel, all agents get same Brief)
                ===================================================
                                              |
                            weather_agent.pitch()
                            reads context["weather_summary"]
                            reads context["notable_facts"]
                            reads context["location_name"]
                            emits 1 Pitch (template hook)
                                              |
                            select_segments() -> running order
```

## Open-Meteo API Integration

**Auth:** None required. Open-Meteo is free and keyless.

**Endpoints:**

`/v1/forecast`:
```
latitude={lat}&longitude={lon}
&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m,visibility
&hourly=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation_probability,precipitation,weather_code,wind_speed_10m,wind_direction_10m,visibility,uv_index
&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset,uv_index_max,precipitation_sum,weather_code
&forecast_days=2
&timezone=auto
```

`/v1/air-quality`:
```
latitude={lat}&longitude={lon}
&current=european_aqi,pm2_5,pm10
&timezone=auto
```

**Weather code mapping** (WMO codes to human-readable):

```python
WMO_CONDITIONS = {
    0: "clear",
    1: "mostly_clear", 2: "partly_cloudy", 3: "overcast",
    45: "fog", 48: "freezing_fog",
    51: "light_drizzle", 53: "drizzle", 55: "heavy_drizzle",
    61: "light_rain", 63: "rain", 65: "heavy_rain",
    71: "light_snow", 73: "snow", 75: "heavy_snow",
    80: "light_showers", 81: "showers", 82: "heavy_showers",
    95: "thunderstorm", 96: "thunderstorm_hail", 99: "heavy_thunderstorm_hail",
}
```

## ScopeContext Shape

```python
class WeatherFact(TypedDict):
    category: str          # "uv" | "wind" | "temperature" | "precipitation" | "air_quality" | "visibility"
    summary: str           # e.g. "UV index peaks at 8 (very high) around 13:00"
                           #      "Rain starting around 23:00, heaviest around 2:00 tomorrow (7.8 mm, 84% chance)"
    severity: str          # "info" | "notable" | "warning"
    hour: int | None       # hour of peak relevance, if applicable

class HourlyForecast(TypedDict):
    hour: int              # 0-23
    temperature_c: float
    feels_like_c: float
    condition: str         # from WMO_CONDITIONS mapping
    precipitation_probability: int  # 0-100
    precipitation_mm: float
    wind_speed_kmh: float
    wind_direction: str    # 8-point compass: N, NE, E, SE, S, SW, W, NW
    visibility_km: float
    uv_index: float
    humidity: int          # 0-100

class CurrentConditions(TypedDict):
    temperature_c: float
    feels_like_c: float
    condition: str
    wind_speed_kmh: float
    wind_direction: str    # 8-point compass from degrees: round(deg/45) % 8
    humidity: int
    visibility_km: float   # Open-Meteo returns meters; divide by 1000
    uv_index: float        # sourced from hourly[current_hour], not current block

class AirQuality(TypedDict):
    aqi: int               # European AQI (Open-Meteo default)
    pm25: float
    pm10: float
    category: str          # "good" | "fair" | "moderate" | "poor" | "very_poor"

class DayPast(TypedDict):
    """What has already happened today. Fields are None when the event is still upcoming."""
    sunrise: str | None              # "06:42" if sunrise_hour <= current_hour, else None
    high_f: float | None             # daily high if current_hour >= 15 (peak likely passed), else None
    precipitation_mm_so_far: float   # sum of hourly precipitation for hours 0..current_hour

class DayAhead(TypedDict):
    """Forward-looking view of what's still coming today. Producer builds segments from this."""
    high_f: float | None             # max temperature across hourly_forecast (next 24h)
    low_f: float | None              # min temperature across hourly_forecast
    hours_remaining: int             # len(hourly_forecast) — may be < 24 at end of day
    sunset: str | None               # "19:58" if sunset_hour > current_hour, else None
    total_precipitation_mm: float    # full-day forecast total (can't be split; not "remaining")
    dominant_condition: str          # full-day dominant condition (WMO-mapped)
    max_uv: float                    # full-day peak UV (may be past or future)

class WeatherScopeContext(TypedDict):
    weather_summary: str              # narrative summary for Brief.today_context
    current: CurrentConditions
    day_past: DayPast                 # replaces old `daily` (time-aware)
    day_ahead: DayAhead               # replaces old `daily` (time-aware)
    air_quality: AirQuality | None    # None when AQ fetch fails; see Fallback Behavior table
    notable_facts: list[WeatherFact]  # top 3 most interesting observations
    location_name: str                # "San Francisco, CA" or similar
    fetched_at: str                   # ISO 8601 timestamp
    # hourly_forecast is computed internally but NOT surfaced — raw hourly
    # data was noise for the Producer; notable_facts + day_ahead are the
    # downstream inputs. The 24h-window parsing lives in _parse_forecast().
```

## Narrative Compiler

Deterministic function, no LLM. Takes weather data, outputs a 2-3 sentence summary highlighting the most radio-interesting facts.

**Length target:** 2 sentences if 0-1 notable facts, 3 sentences if 2+ notable facts. Maximum ~60 words. Always start with current conditions ("Currently XF and [condition]"), then notable facts by severity.

**Time-aware fallback (zero notable facts):** Derive remaining-day high/low from `day_ahead`, not the old full-day `daily` block. Sentence reads "Expecting highs near XF, lows around YF" to signal forward-looking intent. Guard: if `day_ahead.hours_remaining < 2` (late night), suppress the high/low sentence and fall back to "Conditions look steady" — one remaining hour is not a meaningful day picture.

**Interestingness scoring** (per observable):

| Observable | Threshold | Severity |
|------------|-----------|----------|
| Temperature swing > 15F (forward 24h) | day_ahead high-low delta | notable |
| UV index >= 6 | WHO "high" | notable |
| UV index >= 8 | WHO "very high" | warning |
| Wind > 40 km/h | Beaufort 6 "strong breeze" | notable |
| AQI > 50 | European "fair" threshold | notable |
| AQI > 100 | European "poor" | warning |
| Precipitation probability > 60% | qualifying hour | notable |
| Visibility < 5 km | reduced | notable |
| Feels-like diverges from actual by > 5C | current OR max in next 6h | notable |

**Summary phrasing rules:**

- **Precipitation.** Collect hours with `precipitation_probability > 60`. Anchor the fact on the **peak-mm** hour (max `precipitation_mm`; Python `max()` yields the first maximum, giving natural earliest-hour tiebreak). If the first qualifying hour differs from the peak hour, include both: *"Rain starting around H1:00, heaviest around H2:00 (X.X mm, Y% chance)"*. Otherwise report the single hour. Include the `mm` amount only when `>= 0.1`; below that, fall back to the probability-only form.
- **UV suppression.** Skip UV facts when `day_ahead.sunset is None` (sun already set) AND the peak hour has wrapped into tomorrow (see below). Tomorrow's UV preview is stale context at night.
- **Tomorrow labels.** `hourly_forecast` starts at the current hour and wraps past midnight. Any entry whose `hour` is numerically less than `hourly_forecast[0].hour` is **tomorrow**. Summaries must render these as `"{hour}:00 tomorrow"` via `_time_label(hour, first_hour)`. Applied to UV, precipitation (start and peak), and wind summaries.
- **Temperature swing.** Do not use the word "today" — the 24h window straddles midnight at evening runs. Phrase as *"Temperature swings XF, from LF to HF"*.

**Sort order** (fully deterministic): severity (warning > notable > info), then specificity (has specific hour > general), then category in canonical order: precipitation > uv > wind > temperature > air_quality > visibility.

**Example (morning run, current_hour=10):**
- Input: temp 22C, UV 8 at 13:00, rain 70% at 17:00, AQI 35, wind 15 km/h
- Notable facts: [UV very high at 13:00, rain likely at 17:00, temp comfortable at 22C]
- Narrative: "Currently 22C and sunny. UV peaks at 8 around 13:00, so sunscreen's a good call. Rain rolls in around 17:00 with a 70% chance. Otherwise mild with light winds."

**Example (evening run, current_hour=22):**
- Input: drizzle starts 23:00 (0.5mm), peak 02:00 tomorrow (7.8mm, 84%), UV peak 13:00 tomorrow, sunset past
- Notable facts: precipitation (peak-mm anchored) only; UV suppressed
- Narrative: "Currently 70F and partly cloudy. Rain starting around 23:00, heaviest around 2:00 tomorrow (7.8 mm, 84% chance). Temperature swings 30F, from 40F to 70F."

## Pitch Generation

Deterministic. No LLM. Emits exactly 1 Pitch.

**Hook format: structured WHAT / SOURCE / GOAL.** The hook is a creative
brief to the Producer — never spoken verbatim. The what/source/goal
layout forbids treating the hook as speakable prose and tells the
Producer explicitly that `data` is the content source.

```
WHAT: Weather for {location} — {top-3 notable-fact summaries joined by "; "}.
SOURCE: Open-Meteo live feed (listener's GPS location: {location}).
GOAL: Ground the show in today's real-world conditions. Use data.current,
      data.day_ahead, and data.notable_facts as the content source; this
      hook is orientation only.
```

Degraded (no location or forecast-API failure) emits a degraded WHAT
line, `priority=0.3`, `thin_signal=True`. No `rationale` field — agents
stopped emitting it (it was write-only across the codebase). See
`agents/weather/agent.py` for the canonical implementation.

## Location Acquisition

The weather agent reads lat/lon from user profile. The location approval flow is triggered inline during the agent selection flow — step 3 of the sequential auth sequence (after YouTube OAuth and Calendar OAuth). See `agents/docs/DESIGN.md` §Agent Selection & Auth Sequence.

1. User selects Weather agent on the agent selection screen
2. After YouTube and Calendar OAuth complete, browser geolocation prompt fires
3. User approves → browser geolocation API returns lat/lon
4. lat/lon stored in user profile (Supabase `user_profiles` table or in-memory for demo)
5. `fetch_context()` reads `(lat, lon)` from profile
6. Fallback: no location (user denied or skipped Weather) → return `{"weather_summary": None}`

**Location name:** Store city/area name in user profile during approval flow (one-time reverse geocode). Do not reverse-geocode on every `fetch_context()` call.

**Demo moment:** The GPS approval prompt is visible on screen — the third and final permission in the auth sequence. Judge sees user approve location access after YouTube and Calendar, completing the "real agents with real data" thesis. Personalized weather appears in the podcast ~60 seconds later.

## Fallback Behavior

| Condition | Hook WHAT line | Priority | thin_signal |
|-----------|----------------|----------|-------------|
| No location in profile | "Weather segment, degraded (no data available)." | 0.3 | True |
| Forecast API failure (timeout/error) | "Weather segment, degraded (no data available)." | 0.3 | True |
| AQ API fails, forecast succeeds | Normal pitch (AQ omitted from narrative) | 0.5 | False |
| Forecast succeeds, 0 notable facts | "Weather for {loc} — steady conditions, no notable facts" | 0.5 | False |
| Forecast succeeds, notable facts exist | "Weather for {loc} — {top-3 highlights}" | 0.5 | False |

## Implementation Notes

- **HTTP client:** `httpx.Client` (sync) with `timeout=3.0` per call. Sequential calls inside `fetch_context()`.
- **Wind direction:** 8-point compass from degrees: `directions = ["N","NE","E","SE","S","SW","W","NW"]`, `index = round(degrees / 45) % 8`.
- **UV index sourcing:** Not available in Open-Meteo's `current` block. Source from `hourly.uv_index[current_hour_index]`.
- **Visibility units:** Open-Meteo returns meters. Divide by 1000 for `visibility_km`.
- **Hourly window:** `forecast_days=2` returns 48 hours. `timezone=auto` means `hourly.time[0]` is local midnight. Current hour index = current local hour (0-23). Slice `hourly[current_hour_index : current_hour_index + 24]` for the next 24h window.
- **Day-block split:** `_parse_day_blocks(daily, hourly_forecast, past_precip_mm, current_hour)` returns `(day_past, day_ahead)`. Inputs: the already-parsed full-day aggregates, the next-24h hourly slice, and the sum of past hourly precipitation (hours 0..current_hour from `raw_hourly["precipitation"]`). `_PEAK_HOUR_THRESHOLD = 15`: daily high is considered "past" after 15:00 local. Empty `hourly_forecast` (end of day) degrades to `day_ahead.high_f = day_ahead.low_f = None`.
- **Time labels:** `_time_label(hour, first_hour)` renders `"{hour}:00"` or `"{hour}:00 tomorrow"` based on whether the hour has wrapped past midnight. `first_hour = hourly_forecast[0]["hour"]`. Used by UV, precipitation, and wind summaries so the LLM downstream and human readers can't mistake tomorrow for past-today.
- **UV night-suppression:** When `day_ahead.sunset is None` AND UV peak hour `< first_hour` (wrapped into tomorrow), skip the UV fact. Editorial: tomorrow's UV preview is not actionable context for a night-time brief.
- **Timezone assumption:** `_current_hour()` uses `datetime.now().hour` (system local time). Since Open-Meteo is called with `timezone=auto`, its hourly indices match the user's configured location timezone. These can diverge if the system clock timezone differs from the configured location — out of scope for demo.
- **AQI categories:** European AQI: good (0-20), fair (21-40), moderate (41-60), poor (61-80), very_poor (81+).
- **Runtime type note:** `context` in `pitch()` is a plain `dict` at runtime. Use `.get()` with defaults for all field access to handle partial failure cases.

## Orchestrator Contract

The orchestrator copies `WeatherScopeContext["weather_summary"]` into `Brief.today_context["weather_summary"]` after `fetch_context()` returns. The weather agent does not write to `Brief` directly. This matches how the orchestrator works for all context-producing agents.

## Protocol Compliance

- Implements `DataAgent` protocol (`agents/protocol.py`)
- `load_memory()`: returns `bootstrap_memory()` (no weather-specific memory in v0). Weather pitches carry `topic=None` in their `PitchEmission`, so learning-loop does not target weather's `topic_multiplier` even when unstubbed. Weather can still affect `ProducerMemory.agent_weights["weather"]` via per-segment like/replay/skip — but in v0 learning-loop is stubbed (no writes). See `learning_loop/docs/DESIGN.md` §v0 stub contract.
- `fetch_context(user_id)`: returns `ScopeContext` (typed as `WeatherScopeContext`)
- `pitch(brief, memory, context, user_id)`: returns `list[Pitch]` with exactly 1 pitch
- `claim_kind`: always `"neutral"`
- `provenance_shape`: always `"balanced"`
- `thin_signal`: `True` only on degraded (no data), `False` otherwise

## Open Questions

1. **Timezone handling.** Open-Meteo's `timezone=auto` returns local times. Verify that hourly data aligns with user's local clock for "rain at 15:00" statements. Same caveat applies to `_parse_day_blocks` — see Implementation Notes.
2. **Approach C upgrade path.** Weather event classification (`weather_arc`, `weather_event_type`) is a natural v1 extension. The `notable_facts` infrastructure is the foundation for C's event classifier.

## Deferred to v1

- **Weather event classification (Approach C):** `storm_incoming`, `heat_wave`, `perfect_day`, `temperature_swing`, `routine` — as a Pitch field for Producer energy calibration. Natural upgrade from notable-facts ranking.
- **Persistent memory:** Weather-specific memory (e.g., "user lives in a rainy city, don't keep calling rain notable").
- **Multi-location:** Support multiple locations (home, office) for commute-aware weather.

## Dependencies

| Dependency | Version | Purpose |
|------------|---------|---------|
| `httpx` | latest | HTTP client for Open-Meteo API calls |

No additional dependencies. `httpx` is already in the project.

## Test Plan

**Test file:** `tests/test_weather_agent.py`

Key test cases:
- `fetch_context()`: mock `httpx.Client.get()`, verify ScopeContext shape for success, forecast failure, AQ failure, no-location paths
- `narrative_compiler()`: parametrized inputs covering 0/1/2+ notable facts, verify sentence count and word limit
- `notable_facts()`: verify sort order (severity > specificity > category), threshold detection for each observable
- `pitch()`: all branches (no summary, summary without notables, summary with notables), priority and thin_signal values
- WMO code mapping: known codes map correctly, unknown codes fall back gracefully
- Wind direction: parametrized degree inputs map to correct 8-point compass values

"""Tests for weather agent — TDD, inside-out.

Spec: agents/weather/docs/DESIGN.md
"""

from __future__ import annotations

import pytest


# ── Helper tests: WMO codes, wind direction, AQI category ──


class TestWmoConditions:
    """WMO weather code → human-readable string."""

    @pytest.mark.parametrize(
        "code, expected",
        [
            (0, "clear"),
            (1, "mostly_clear"),
            (2, "partly_cloudy"),
            (3, "overcast"),
            (45, "fog"),
            (48, "freezing_fog"),
            (51, "light_drizzle"),
            (53, "drizzle"),
            (55, "heavy_drizzle"),
            (61, "light_rain"),
            (63, "rain"),
            (65, "heavy_rain"),
            (71, "light_snow"),
            (73, "snow"),
            (75, "heavy_snow"),
            (80, "light_showers"),
            (81, "showers"),
            (82, "heavy_showers"),
            (95, "thunderstorm"),
            (96, "thunderstorm_hail"),
            (99, "heavy_thunderstorm_hail"),
        ],
    )
    def test_known_codes(self, code, expected):
        from agents.weather.agent import wmo_condition
        assert wmo_condition(code) == expected

    def test_unknown_code_returns_fallback(self):
        from agents.weather.agent import wmo_condition
        assert wmo_condition(999) == "unknown"


class TestWindDirection:
    """Degrees → 8-point compass."""

    @pytest.mark.parametrize(
        "degrees, expected",
        [
            (0, "N"),
            (45, "NE"),
            (90, "E"),
            (135, "SE"),
            (180, "S"),
            (225, "SW"),
            (270, "W"),
            (315, "NW"),
            (360, "N"),       # wrap-around
            (22, "N"),        # just under NE boundary
            (23, "NE"),       # just over NE boundary
            (337, "NW"),      # just under N boundary
            (338, "N"),       # wrap to N
        ],
    )
    def test_degrees_to_compass(self, degrees, expected):
        from agents.weather.agent import wind_direction
        assert wind_direction(degrees) == expected


class TestAqiCategory:
    """European AQI → category string."""

    @pytest.mark.parametrize(
        "aqi, expected",
        [
            (0, "good"),
            (20, "good"),
            (21, "fair"),
            (40, "fair"),
            (41, "moderate"),
            (60, "moderate"),
            (61, "poor"),
            (80, "poor"),
            (81, "very_poor"),
            (150, "very_poor"),
        ],
    )
    def test_aqi_categories(self, aqi, expected):
        from agents.weather.agent import aqi_category
        assert aqi_category(aqi) == expected


# ── Day-blocks parser tests ──


class TestParseDayBlocks:
    """_parse_day_blocks splits daily data into day_past + day_ahead."""

    def _daily(self, **overrides) -> dict:
        base = {
            "high_f": 75.0,
            "low_f": 55.0,
            "sunrise": "06:30",
            "sunset": "19:45",
            "max_uv": 7.0,
            "total_precipitation_mm": 3.0,
            "dominant_condition": "partly_cloudy",
        }
        base.update(overrides)
        return base

    def _hourly(self, temps: list[float]) -> list[dict]:
        return [
            {
                "hour": i,
                "temperature_f": t,
                "feels_like_f": t,
                "condition": "clear",
                "precipitation_probability": 0,
                "precipitation_mm": 0.0,
                "wind_speed_kmh": 10.0,
                "wind_direction": "N",
                "visibility_km": 10.0,
                "uv_index": 3.0,
                "humidity": 50,
            }
            for i, t in enumerate(temps)
        ]

    def test_sunrise_past_populates_day_past(self):
        from agents.weather.agent import _parse_day_blocks
        day_past, _ = _parse_day_blocks(
            self._daily(sunrise="06:30"), self._hourly([70.0] * 10), 0.0, current_hour=10
        )
        assert day_past["sunrise"] == "06:30"

    def test_sunrise_upcoming_none_in_day_past(self):
        from agents.weather.agent import _parse_day_blocks
        day_past, _ = _parse_day_blocks(
            self._daily(sunrise="06:30"), self._hourly([70.0] * 2), 0.0, current_hour=5
        )
        assert day_past["sunrise"] is None

    def test_high_f_populated_after_peak_hour(self):
        from agents.weather.agent import _parse_day_blocks
        day_past, _ = _parse_day_blocks(
            self._daily(high_f=75.0), self._hourly([68.0] * 5), 0.0, current_hour=15
        )
        assert day_past["high_f"] == 75.0

    def test_high_f_none_before_peak_hour(self):
        from agents.weather.agent import _parse_day_blocks
        day_past, _ = _parse_day_blocks(
            self._daily(high_f=75.0), self._hourly([68.0] * 14), 0.0, current_hour=10
        )
        assert day_past["high_f"] is None

    def test_precipitation_mm_so_far(self):
        from agents.weather.agent import _parse_day_blocks
        day_past, _ = _parse_day_blocks(
            self._daily(), self._hourly([68.0] * 20), past_precip_mm=2.5, current_hour=4
        )
        assert day_past["precipitation_mm_so_far"] == 2.5

    def test_day_ahead_high_is_hourly_max(self):
        from agents.weather.agent import _parse_day_blocks
        _, day_ahead = _parse_day_blocks(
            self._daily(), self._hourly([60.0, 65.0, 72.0, 68.0, 63.0]), 0.0, current_hour=12
        )
        assert day_ahead["high_f"] == 72.0

    def test_day_ahead_low_is_hourly_min(self):
        from agents.weather.agent import _parse_day_blocks
        _, day_ahead = _parse_day_blocks(
            self._daily(), self._hourly([60.0, 65.0, 72.0, 68.0, 55.0]), 0.0, current_hour=12
        )
        assert day_ahead["low_f"] == 55.0

    def test_sunset_upcoming_in_day_ahead(self):
        from agents.weather.agent import _parse_day_blocks
        _, day_ahead = _parse_day_blocks(
            self._daily(sunset="19:45"), self._hourly([68.0] * 8), 0.0, current_hour=12
        )
        assert day_ahead["sunset"] == "19:45"

    def test_sunset_past_none_in_day_ahead(self):
        from agents.weather.agent import _parse_day_blocks
        _, day_ahead = _parse_day_blocks(
            self._daily(sunset="19:45"), self._hourly([62.0] * 3), 0.0, current_hour=21
        )
        assert day_ahead["sunset"] is None

    def test_hours_remaining_matches_hourly_len(self):
        from agents.weather.agent import _parse_day_blocks
        _, day_ahead = _parse_day_blocks(
            self._daily(), self._hourly([68.0] * 7), 0.0, current_hour=17
        )
        assert day_ahead["hours_remaining"] == 7

    def test_day_ahead_carries_full_day_aggregates(self):
        from agents.weather.agent import _parse_day_blocks
        _, day_ahead = _parse_day_blocks(
            self._daily(total_precipitation_mm=3.0, dominant_condition="rain", max_uv=7.5),
            self._hourly([68.0] * 6),
            0.0,
            current_hour=18,
        )
        assert day_ahead["total_precipitation_mm"] == 3.0
        assert day_ahead["dominant_condition"] == "rain"
        assert day_ahead["max_uv"] == 7.5

    def test_empty_hourly_forecast_degrades_gracefully(self):
        from agents.weather.agent import _parse_day_blocks
        _, day_ahead = _parse_day_blocks(
            self._daily(), [], 0.0, current_hour=23
        )
        assert day_ahead["high_f"] is None
        assert day_ahead["low_f"] is None
        assert day_ahead["hours_remaining"] == 0


# ── Notable facts tests ──


def _make_weather_data(
    *,
    current_temp: float = 20.0,
    current_feels_like: float = 20.0,
    high: float = 23.0,
    low: float = 17.0,
    hourly_uv: list[float] | None = None,
    hourly_precip_prob: list[int] | None = None,
    hourly_wind: list[float] | None = None,
    hourly_visibility: list[float] | None = None,
    current_wind: float = 10.0,
    current_visibility: float = 10.0,
    aqi: int | None = None,
) -> dict:
    """Build a minimal weather data dict for notable_facts / narrative_compiler input."""
    hours = 24
    hourly_temps = [68.0] * hours
    return {
        "current": {
            "temperature_f": current_temp,
            "feels_like_f": current_feels_like,
            "condition": "clear",
            "wind_speed_kmh": current_wind,
            "wind_direction": "N",
            "humidity": 50,
            "visibility_km": current_visibility,
            "uv_index": (hourly_uv or [3.0] * hours)[0],
        },
        "hourly_forecast": [
            {
                "hour": h,
                "temperature_f": hourly_temps[h],
                "feels_like_f": hourly_temps[h],
                "condition": "clear",
                "precipitation_probability": (hourly_precip_prob or [0] * hours)[h],
                "precipitation_mm": 0.0,
                "wind_speed_kmh": (hourly_wind or [10.0] * hours)[h],
                "wind_direction": "N",
                "visibility_km": (hourly_visibility or [10.0] * hours)[h],
                "uv_index": (hourly_uv or [3.0] * hours)[h],
                "humidity": 50,
            }
            for h in range(hours)
        ],
        # day_past / day_ahead replace the old daily block.
        # Fixture assumes current_hour=10 (morning): high not yet peaked, sunrise past.
        "day_past": {
            "sunrise": "06:30",
            "high_f": None,           # hour < 15, peak not yet reached
            "precipitation_mm_so_far": 0.0,
        },
        "day_ahead": {
            "high_f": high,
            "low_f": low,
            "hours_remaining": hours,
            "sunset": "19:30",
            "total_precipitation_mm": 0.0,
            "dominant_condition": "clear",
            "max_uv": max(hourly_uv or [3.0]),
        },
        "air_quality": (
            {"aqi": aqi, "pm25": 10.0, "pm10": 15.0, "category": "good"}
            if aqi is not None
            else None
        ),
    }


class TestNotableFacts:
    """Deterministic notable-facts extraction and ranking."""

    def test_no_notable_facts_for_calm_weather(self):
        from agents.weather.agent import notable_facts
        data = _make_weather_data()
        facts = notable_facts(data)
        assert facts == []

    def test_high_uv_detected(self):
        from agents.weather.agent import notable_facts
        uv = [2.0] * 10 + [8.0] + [2.0] * 13  # UV 8 at hour 10
        data = _make_weather_data(hourly_uv=uv)
        facts = notable_facts(data)
        uv_facts = [f for f in facts if f["category"] == "uv"]
        assert len(uv_facts) == 1
        assert uv_facts[0]["severity"] == "warning"
        assert uv_facts[0]["hour"] == 10

    def test_notable_uv_threshold(self):
        """UV 6-7 is 'notable', not 'warning'."""
        from agents.weather.agent import notable_facts
        uv = [2.0] * 12 + [6.5] + [2.0] * 11
        data = _make_weather_data(hourly_uv=uv)
        facts = notable_facts(data)
        uv_facts = [f for f in facts if f["category"] == "uv"]
        assert len(uv_facts) == 1
        assert uv_facts[0]["severity"] == "notable"

    def test_precipitation_detected(self):
        from agents.weather.agent import notable_facts
        precip = [0] * 17 + [70] + [0] * 6  # 70% at hour 17
        data = _make_weather_data(hourly_precip_prob=precip)
        facts = notable_facts(data)
        precip_facts = [f for f in facts if f["category"] == "precipitation"]
        assert len(precip_facts) == 1
        assert precip_facts[0]["hour"] == 17

    def test_precipitation_peak_mm_selection(self):
        """With multiple qualifying hours, surface the peak-mm hour, not the first.

        Mirrors a real Open-Meteo response: drizzle at hour 0 (0.7mm, 68%),
        heavy rain at hour 2 (7.8mm, 84%). The 7.8mm hour is the story.
        """
        from agents.weather.agent import notable_facts
        data = _make_weather_data()
        data["hourly_forecast"][0].update({"precipitation_probability": 68, "precipitation_mm": 0.7})
        data["hourly_forecast"][1].update({"precipitation_probability": 87, "precipitation_mm": 0.2})
        data["hourly_forecast"][2].update({"precipitation_probability": 84, "precipitation_mm": 7.8})
        data["hourly_forecast"][3].update({"precipitation_probability": 76, "precipitation_mm": 0.1})
        data["hourly_forecast"][4].update({"precipitation_probability": 68, "precipitation_mm": 0.8})
        facts = notable_facts(data)
        precip = next(f for f in facts if f["category"] == "precipitation")
        # Notable anchor is the peak-mm hour (2), not the first qualifying hour (0)
        assert precip["hour"] == 2
        # Summary mentions start (0:00) AND peak (2:00), the mm amount, and peak prob
        assert "0:00" in precip["summary"]
        assert "2:00" in precip["summary"]
        assert "7.8" in precip["summary"]
        assert "84" in precip["summary"]

    def test_precipitation_single_qualifying_hour_no_start_note(self):
        """One qualifying hour: summary does not mention a separate start."""
        from agents.weather.agent import notable_facts
        data = _make_weather_data()
        data["hourly_forecast"][15].update({"precipitation_probability": 75, "precipitation_mm": 3.2})
        facts = notable_facts(data)
        precip = next(f for f in facts if f["category"] == "precipitation")
        assert precip["hour"] == 15
        assert "3.2" in precip["summary"]
        assert "75" in precip["summary"]
        assert "start" not in precip["summary"].lower()
        assert "heaviest" not in precip["summary"].lower()

    def test_precipitation_contiguous_same_hour_peak_and_start(self):
        """When peak-mm hour is also the first qualifying hour, no start/peak split."""
        from agents.weather.agent import notable_facts
        data = _make_weather_data()
        data["hourly_forecast"][10].update({"precipitation_probability": 90, "precipitation_mm": 5.0})
        data["hourly_forecast"][11].update({"precipitation_probability": 75, "precipitation_mm": 1.5})
        facts = notable_facts(data)
        precip = next(f for f in facts if f["category"] == "precipitation")
        assert precip["hour"] == 10
        assert "start" not in precip["summary"].lower()
        assert "heaviest" not in precip["summary"].lower()

    def test_uv_hour_labeled_tomorrow_when_wrapped(self):
        """When hourly_forecast wraps past midnight, UV peak summary says 'tomorrow'."""
        from agents.weather.agent import notable_facts
        data = _make_weather_data()
        # Rotate to simulate current_hour=22: hours 22,23,0,1,...,21
        rotated = [dict(h) for h in data["hourly_forecast"]]
        for idx, h in enumerate(rotated):
            h["hour"] = (22 + idx) % 24
        data["hourly_forecast"] = rotated
        # UV peak at sequence index 15 -> hour (22+15)%24 = 13 (tomorrow)
        rotated[15]["uv_index"] = 8.0
        # Keep sunset upcoming so UV isn't suppressed
        data["day_ahead"]["sunset"] = "19:30"
        facts = notable_facts(data)
        uv = next(f for f in facts if f["category"] == "uv")
        assert "13:00 tomorrow" in uv["summary"]

    def test_uv_hour_no_tomorrow_when_same_day(self):
        """UV peak today — no 'tomorrow' suffix."""
        from agents.weather.agent import notable_facts
        uv = [2.0] * 10 + [8.0] + [2.0] * 13
        data = _make_weather_data(hourly_uv=uv)
        facts = notable_facts(data)
        uv_fact = next(f for f in facts if f["category"] == "uv")
        assert "tomorrow" not in uv_fact["summary"]

    def test_uv_suppressed_when_sunset_past_and_peak_tomorrow(self):
        """At night with UV peak wrapped into tomorrow, the fact is suppressed as stale."""
        from agents.weather.agent import notable_facts
        data = _make_weather_data()
        rotated = [dict(h) for h in data["hourly_forecast"]]
        for idx, h in enumerate(rotated):
            h["hour"] = (22 + idx) % 24
        data["hourly_forecast"] = rotated
        rotated[15]["uv_index"] = 8.0  # hour 13 tomorrow
        data["day_ahead"]["sunset"] = None  # sunset past
        facts = notable_facts(data)
        uv_facts = [f for f in facts if f["category"] == "uv"]
        assert uv_facts == []

    def test_uv_kept_when_sunset_past_but_peak_today(self):
        """If UV peak is still today (hasn't wrapped), keep it even after sunset."""
        from agents.weather.agent import notable_facts
        data = _make_weather_data()
        rotated = [dict(h) for h in data["hourly_forecast"]]
        for idx, h in enumerate(rotated):
            h["hour"] = (22 + idx) % 24
        data["hourly_forecast"] = rotated
        rotated[0]["uv_index"] = 8.0  # hour 22 today (edge case, keeps logic honest)
        data["day_ahead"]["sunset"] = None
        facts = notable_facts(data)
        uv_facts = [f for f in facts if f["category"] == "uv"]
        assert len(uv_facts) == 1

    def test_precipitation_wrapped_hours_labeled_tomorrow(self):
        """Precip hours that wrap past midnight get 'tomorrow' labels."""
        from agents.weather.agent import notable_facts
        data = _make_weather_data()
        rotated = [dict(h) for h in data["hourly_forecast"]]
        for idx, h in enumerate(rotated):
            h["hour"] = (22 + idx) % 24
        data["hourly_forecast"] = rotated
        # Start at sequence idx 2 (hour 0 tomorrow), peak at idx 4 (hour 2 tomorrow)
        rotated[2].update({"precipitation_probability": 68, "precipitation_mm": 0.7})
        rotated[3].update({"precipitation_probability": 87, "precipitation_mm": 0.2})
        rotated[4].update({"precipitation_probability": 84, "precipitation_mm": 7.8})
        facts = notable_facts(data)
        precip = next(f for f in facts if f["category"] == "precipitation")
        assert "0:00 tomorrow" in precip["summary"]
        assert "2:00 tomorrow" in precip["summary"]

    def test_temperature_swing_summary_drops_today(self):
        """Temp swing summary no longer includes 'today' — data may span midnight."""
        from agents.weather.agent import notable_facts
        data = _make_weather_data(high=90.0, low=60.0)
        facts = notable_facts(data)
        temp = next(f for f in facts if "swings" in f["summary"].lower())
        assert "today" not in temp["summary"].lower()

    def test_high_wind_detected(self):
        from agents.weather.agent import notable_facts
        wind = [10.0] * 14 + [45.0] + [10.0] * 9  # 45 km/h at hour 14
        data = _make_weather_data(hourly_wind=wind)
        facts = notable_facts(data)
        wind_facts = [f for f in facts if f["category"] == "wind"]
        assert len(wind_facts) == 1
        assert wind_facts[0]["severity"] == "notable"

    def test_temperature_swing_detected(self):
        from agents.weather.agent import notable_facts
        data = _make_weather_data(high=90.0, low=60.0)  # 30F swing
        facts = notable_facts(data)
        temp_facts = [f for f in facts if f["category"] == "temperature"]
        assert len(temp_facts) == 1
        assert temp_facts[0]["severity"] == "notable"

    def test_no_temp_swing_under_threshold(self):
        from agents.weather.agent import notable_facts
        data = _make_weather_data(high=77.0, low=68.0)  # 9F, under 15F threshold
        facts = notable_facts(data)
        temp_facts = [f for f in facts if f["category"] == "temperature"]
        assert temp_facts == []

    def test_poor_aqi_detected(self):
        from agents.weather.agent import notable_facts
        data = _make_weather_data(aqi=110)
        facts = notable_facts(data)
        aq_facts = [f for f in facts if f["category"] == "air_quality"]
        assert len(aq_facts) == 1
        assert aq_facts[0]["severity"] == "warning"

    def test_fair_aqi_notable(self):
        from agents.weather.agent import notable_facts
        data = _make_weather_data(aqi=55)
        facts = notable_facts(data)
        aq_facts = [f for f in facts if f["category"] == "air_quality"]
        assert len(aq_facts) == 1
        assert aq_facts[0]["severity"] == "notable"

    def test_good_aqi_not_flagged(self):
        from agents.weather.agent import notable_facts
        data = _make_weather_data(aqi=30)
        facts = notable_facts(data)
        aq_facts = [f for f in facts if f["category"] == "air_quality"]
        assert aq_facts == []

    def test_low_visibility_detected(self):
        from agents.weather.agent import notable_facts
        vis = [3.0] * 6 + [10.0] * 18  # low visibility early morning
        data = _make_weather_data(hourly_visibility=vis, current_visibility=3.0)
        facts = notable_facts(data)
        vis_facts = [f for f in facts if f["category"] == "visibility"]
        assert len(vis_facts) == 1

    def test_feels_like_divergence_detected(self):
        from agents.weather.agent import notable_facts
        data = _make_weather_data(current_temp=68.0, current_feels_like=55.0)  # 13F gap
        facts = notable_facts(data)
        temp_facts = [f for f in facts if f["category"] == "temperature"]
        assert any("feels" in f["summary"].lower() for f in temp_facts)

    def test_sort_order_warning_before_notable(self):
        """Warning severity sorts before notable."""
        from agents.weather.agent import notable_facts
        # UV warning (8) + precip notable (65%)
        uv = [2.0] * 13 + [8.0] + [2.0] * 10
        precip = [0] * 17 + [65] + [0] * 6
        data = _make_weather_data(hourly_uv=uv, hourly_precip_prob=precip)
        facts = notable_facts(data)
        assert len(facts) >= 2
        assert facts[0]["severity"] == "warning"

    def test_sort_order_category_tiebreak(self):
        """When severity and specificity match, canonical category order applies:
        precipitation > uv > wind > temperature > air_quality > visibility."""
        from agents.weather.agent import notable_facts
        # Both notable: precip 65% at hour 17, wind 45 at hour 14
        precip = [0] * 17 + [65] + [0] * 6
        wind = [10.0] * 14 + [45.0] + [10.0] * 9
        data = _make_weather_data(hourly_precip_prob=precip, hourly_wind=wind)
        facts = notable_facts(data)
        categories = [f["category"] for f in facts]
        assert categories.index("precipitation") < categories.index("wind")

    def test_returns_top_3_only(self):
        """At most 3 facts returned."""
        from agents.weather.agent import notable_facts
        # Trigger many facts: UV high, precip, wind, temp swing, poor AQI
        uv = [2.0] * 13 + [8.0] + [2.0] * 10
        precip = [0] * 17 + [70] + [0] * 6
        wind = [10.0] * 14 + [45.0] + [10.0] * 9
        data = _make_weather_data(
            hourly_uv=uv,
            hourly_precip_prob=precip,
            hourly_wind=wind,
            high=32.0, low=18.0,
            aqi=110,
        )
        facts = notable_facts(data)
        assert len(facts) <= 3

    def test_no_air_quality_data_skips_aq_facts(self):
        """When air_quality is None, no AQ facts are produced."""
        from agents.weather.agent import notable_facts
        data = _make_weather_data(aqi=None)
        facts = notable_facts(data)
        aq_facts = [f for f in facts if f["category"] == "air_quality"]
        assert aq_facts == []

    def test_aqi_summary_uses_correct_category_name(self):
        """AQI summary text must use aqi_category(), not hardcoded strings.

        AQI 70 is European 'poor', not 'moderate'.
        AQI 105 is European 'very poor', not 'poor'.
        """
        from agents.weather.agent import notable_facts
        data_70 = _make_weather_data(aqi=70)
        facts_70 = notable_facts(data_70)
        aq_fact = next(f for f in facts_70 if f["category"] == "air_quality")
        assert "poor" in aq_fact["summary"]
        assert "moderate" not in aq_fact["summary"]

        data_105 = _make_weather_data(aqi=105)
        facts_105 = notable_facts(data_105)
        aq_fact_105 = next(f for f in facts_105 if f["category"] == "air_quality")
        assert "very poor" in aq_fact_105["summary"]
        assert aq_fact_105["severity"] == "warning"

    def test_feels_like_divergence_in_next_6h(self):
        """Feels-like divergence in forecast window (next 6h) triggers the fact."""
        from agents.weather.agent import notable_facts
        # Current: no divergence. Hour 3: large wind-chill divergence.
        data = _make_weather_data(current_temp=68.0, current_feels_like=68.0)
        # Inject a 14F divergence at hour 3 in the hourly forecast
        data["hourly_forecast"][3]["temperature_f"] = 64.0
        data["hourly_forecast"][3]["feels_like_f"] = 50.0
        facts = notable_facts(data)
        temp_facts = [f for f in facts if f["category"] == "temperature"]
        feels_facts = [f for f in temp_facts if "feels" in f["summary"].lower()]
        assert len(feels_facts) == 1
        assert feels_facts[0]["hour"] == 3


# ── Narrative compiler tests ──


class TestNarrativeCompiler:
    """Deterministic narrative summary from weather data."""

    def test_starts_with_current_conditions(self):
        from agents.weather.agent import narrative_compiler
        data = _make_weather_data()
        narrative = narrative_compiler(data)
        assert narrative.startswith("Currently")

    def test_two_sentences_with_zero_notable_facts(self):
        """0-1 notable facts -> 2 sentences max."""
        from agents.weather.agent import narrative_compiler
        data = _make_weather_data()
        narrative = narrative_compiler(data)
        sentences = [s.strip() for s in narrative.split(".") if s.strip()]
        assert len(sentences) <= 2

    def test_three_sentences_with_multiple_notable_facts(self):
        """2+ notable facts -> up to 3 sentences."""
        from agents.weather.agent import narrative_compiler
        uv = [2.0] * 13 + [8.0] + [2.0] * 10
        precip = [0] * 17 + [70] + [0] * 6
        data = _make_weather_data(hourly_uv=uv, hourly_precip_prob=precip)
        narrative = narrative_compiler(data)
        sentences = [s.strip() for s in narrative.split(".") if s.strip()]
        assert 2 <= len(sentences) <= 3

    def test_word_count_under_60(self):
        """Narrative must be ~60 words max."""
        from agents.weather.agent import narrative_compiler
        uv = [2.0] * 13 + [8.0] + [2.0] * 10
        precip = [0] * 17 + [70] + [0] * 6
        wind = [10.0] * 14 + [45.0] + [10.0] * 9
        data = _make_weather_data(
            hourly_uv=uv, hourly_precip_prob=precip, hourly_wind=wind,
            high=32.0, low=18.0, aqi=110,
        )
        narrative = narrative_compiler(data)
        word_count = len(narrative.split())
        assert word_count <= 65  # small tolerance

    def test_includes_temperature(self):
        from agents.weather.agent import narrative_compiler
        data = _make_weather_data(current_temp=22.0)
        narrative = narrative_compiler(data)
        assert "22" in narrative

    def test_includes_condition(self):
        from agents.weather.agent import narrative_compiler
        data = _make_weather_data()
        narrative = narrative_compiler(data)
        # condition is "clear" from fixture
        assert "clear" in narrative.lower()


# ── fetch_context tests (mocked httpx) ──

# Realistic Open-Meteo response fixtures

FORECAST_RESPONSE = {
    "current": {
        "temperature_2m": 22.0,
        "relative_humidity_2m": 55,
        "apparent_temperature": 21.0,
        "weather_code": 2,
        "wind_speed_10m": 15.0,
        "wind_direction_10m": 180,
        "visibility": 10000,  # meters
    },
    "hourly": {
        "time": [f"2026-04-16T{h:02d}:00" for h in range(48)],
        "temperature_2m": [20.0 + (h % 10) for h in range(48)],
        "relative_humidity_2m": [55] * 48,
        "apparent_temperature": [19.0 + (h % 10) for h in range(48)],
        "precipitation_probability": [0] * 48,
        "precipitation": [0.0] * 48,
        "weather_code": [2] * 48,
        "wind_speed_10m": [15.0] * 48,
        "wind_direction_10m": [180] * 48,
        "visibility": [10000] * 48,
        "uv_index": [3.0] * 48,
    },
    "daily": {
        "temperature_2m_max": [26.0],
        "temperature_2m_min": [18.0],
        "sunrise": ["2026-04-16T06:42"],
        "sunset": ["2026-04-16T19:58"],
        "uv_index_max": [5.0],
        "precipitation_sum": [0.0],
        "weather_code": [2],
    },
}

AIR_QUALITY_RESPONSE = {
    "current": {
        "european_aqi": 35,
        "pm2_5": 12.0,
        "pm10": 18.0,
    },
}


def _mock_httpx_get(forecast_response=None, aq_response=None,
                    forecast_error=False, aq_error=False):
    """Create a mock httpx.Client.get that returns Open-Meteo responses."""
    from unittest.mock import MagicMock
    import httpx

    def side_effect(url, params=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if "air-quality" in url:
            if aq_error:
                raise httpx.HTTPError("AQ API error")
            resp.json.return_value = aq_response or AIR_QUALITY_RESPONSE
        else:
            if forecast_error:
                raise httpx.HTTPError("Forecast API error")
            resp.json.return_value = forecast_response or FORECAST_RESPONSE
        return resp

    return side_effect


class TestFetchContext:
    """fetch_context with mocked HTTP calls."""

    def test_success_returns_scope_context(self):
        from unittest.mock import patch, MagicMock
        from agents.weather.agent import WeatherAgent

        agent = WeatherAgent()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get = MagicMock(side_effect=_mock_httpx_get())

        with patch("agents.weather.agent.httpx.Client", return_value=mock_client):
            with patch("agents.weather.agent._get_user_location",
                       return_value=(37.7749, -122.4194, "San Francisco, CA")):
                ctx = agent.fetch_context("user1")

        assert ctx["weather_summary"] is not None
        assert isinstance(ctx["weather_summary"], str)
        assert "current" in ctx
        assert "day_past" in ctx
        assert "day_ahead" in ctx
        assert "daily" not in ctx  # replaced by the two split blocks
        assert "hourly_forecast" not in ctx  # dropped — consumed internally
        assert "notable_facts" in ctx
        assert "location_name" in ctx
        assert ctx["location_name"] == "San Francisco, CA"
        assert "fetched_at" in ctx

    def test_no_location_returns_none_summary(self):
        from unittest.mock import patch
        from agents.weather.agent import WeatherAgent

        agent = WeatherAgent()
        with patch("agents.weather.agent._get_user_location", return_value=None):
            ctx = agent.fetch_context("user1")

        assert ctx["weather_summary"] is None

    def test_forecast_failure_returns_none_summary(self):
        from unittest.mock import patch, MagicMock
        from agents.weather.agent import WeatherAgent

        agent = WeatherAgent()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get = MagicMock(side_effect=_mock_httpx_get(forecast_error=True))

        with patch("agents.weather.agent.httpx.Client", return_value=mock_client):
            with patch("agents.weather.agent._get_user_location",
                       return_value=(37.7749, -122.4194, "San Francisco, CA")):
                ctx = agent.fetch_context("user1")

        assert ctx["weather_summary"] is None

    def test_aq_failure_still_succeeds(self):
        """AQ failure is non-fatal; forecast data still returned."""
        from unittest.mock import patch, MagicMock
        from agents.weather.agent import WeatherAgent

        agent = WeatherAgent()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get = MagicMock(side_effect=_mock_httpx_get(aq_error=True))

        with patch("agents.weather.agent.httpx.Client", return_value=mock_client):
            with patch("agents.weather.agent._get_user_location",
                       return_value=(37.7749, -122.4194, "San Francisco, CA")):
                ctx = agent.fetch_context("user1")

        assert ctx["weather_summary"] is not None
        assert ctx["air_quality"] is None

    def test_current_conditions_parsed(self):
        from unittest.mock import patch, MagicMock
        from agents.weather.agent import WeatherAgent

        agent = WeatherAgent()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get = MagicMock(side_effect=_mock_httpx_get())

        with patch("agents.weather.agent.httpx.Client", return_value=mock_client):
            with patch("agents.weather.agent._get_user_location",
                       return_value=(37.7749, -122.4194, "San Francisco, CA")):
                ctx = agent.fetch_context("user1")

        current = ctx["current"]
        assert current["temperature_f"] == 22.0
        assert current["feels_like_f"] == 21.0
        assert current["condition"] == "partly_cloudy"
        assert current["wind_speed_kmh"] == 15.0
        assert current["wind_direction"] == "S"  # 180 degrees
        assert current["humidity"] == 55
        assert current["visibility_km"] == 10.0  # 10000m / 1000

    def test_day_past_and_day_ahead_shape(self):
        """fetch_context produces day_past + day_ahead with time-aware fields."""
        from unittest.mock import patch, MagicMock
        from agents.weather.agent import WeatherAgent

        agent = WeatherAgent()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get = MagicMock(side_effect=_mock_httpx_get())

        with patch("agents.weather.agent.httpx.Client", return_value=mock_client):
            with patch("agents.weather.agent._get_user_location",
                       return_value=(37.7749, -122.4194, "San Francisco, CA")):
                with patch("agents.weather.agent._current_hour", return_value=17):
                    ctx = agent.fetch_context("user1")

        # At 17:00: sunrise (06:42) is past, sunset (19:58) is upcoming,
        # high likely peaked (current_hour >= 15).
        day_past = ctx["day_past"]
        day_ahead = ctx["day_ahead"]

        assert day_past["sunrise"] == "06:42"
        assert day_past["high_f"] == 26.0
        assert day_past["precipitation_mm_so_far"] == 0.0  # FORECAST_RESPONSE has 0 precip

        assert day_ahead["sunset"] == "19:58"
        assert day_ahead["hours_remaining"] == 24
        assert day_ahead["high_f"] is not None
        assert day_ahead["low_f"] is not None
        assert day_ahead["total_precipitation_mm"] == 0.0
        assert day_ahead["dominant_condition"] == "partly_cloudy"
        assert day_ahead["max_uv"] == 5.0

    def test_day_past_early_morning(self):
        """Before sunrise and peak: sunrise None, high_f None, sunset upcoming."""
        from unittest.mock import patch, MagicMock
        from agents.weather.agent import WeatherAgent

        agent = WeatherAgent()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get = MagicMock(side_effect=_mock_httpx_get())

        with patch("agents.weather.agent.httpx.Client", return_value=mock_client):
            with patch("agents.weather.agent._get_user_location",
                       return_value=(37.7749, -122.4194, "San Francisco, CA")):
                with patch("agents.weather.agent._current_hour", return_value=4):
                    ctx = agent.fetch_context("user1")

        day_past = ctx["day_past"]
        day_ahead = ctx["day_ahead"]

        assert day_past["sunrise"] is None
        assert day_past["high_f"] is None
        assert day_ahead["sunset"] == "19:58"

# hourly_forecast is computed internally (drives notable_facts and the
# narrative compiler) but is no longer surfaced in ScopeContext or
# Pitch.data — the 24h-window shape is exercised via notable_facts tests.


# ── Pitch tests ──


class TestPitch:
    """pitch() emits exactly 1 Pitch with correct fields per branch."""

    def _make_brief(self) -> dict:
        return {
            "today_context": {
                "date": "2026-04-16",
                "day_of_week": "Wednesday",
                "time_of_day": "morning",
                "weather_summary": None,
                "calendar_events": None,
            }
        }

    def test_no_summary_degraded_pitch(self):
        from agents.weather.agent import WeatherAgent
        from agents.protocol import bootstrap_memory

        agent = WeatherAgent()
        ctx = {"weather_summary": None}
        pitches = agent.pitch(self._make_brief(), bootstrap_memory(), ctx, "user1")

        assert len(pitches) == 1
        p = pitches[0]
        assert p["agent"] == "weather"
        assert p["priority"] == 0.3
        assert p["thin_signal"] is True
        assert p["claim_kind"] == "neutral"
        assert "degraded" in p["hook"].lower()
        assert "no data available" in p["hook"].lower()

    def test_summary_without_notables(self):
        from agents.weather.agent import WeatherAgent
        from agents.protocol import bootstrap_memory

        agent = WeatherAgent()
        ctx = {
            "weather_summary": "Currently 22C and clear. Highs near 23C, lows around 17C.",
            "notable_facts": [],
            "location_name": "San Francisco, CA",
        }
        pitches = agent.pitch(self._make_brief(), bootstrap_memory(), ctx, "user1")

        assert len(pitches) == 1
        p = pitches[0]
        assert p["priority"] == 0.5
        assert p["thin_signal"] is False
        assert "San Francisco" in p["title"]

    def test_summary_with_notables(self):
        from agents.weather.agent import WeatherAgent
        from agents.protocol import bootstrap_memory

        agent = WeatherAgent()
        facts = [
            {"category": "uv", "summary": "UV peaks at 8 around 13:00", "severity": "warning", "hour": 13},
            {"category": "precipitation", "summary": "Rain 70% at 17:00", "severity": "notable", "hour": 17},
        ]
        ctx = {
            "weather_summary": "Currently 22C and sunny. UV peaks at 8 around 13:00.",
            "notable_facts": facts,
            "location_name": "San Francisco, CA",
        }
        pitches = agent.pitch(self._make_brief(), bootstrap_memory(), ctx, "user1")

        assert len(pitches) == 1
        p = pitches[0]
        assert p["priority"] == 0.5
        assert p["thin_signal"] is False
        # Hook should include notable fact summaries
        assert "UV" in p["hook"]
        assert "Rain" in p["hook"]

    def test_always_neutral_claim_kind(self):
        from agents.weather.agent import WeatherAgent
        from agents.protocol import bootstrap_memory

        agent = WeatherAgent()
        ctx = {
            "weather_summary": "Currently 22C and clear.",
            "notable_facts": [],
            "location_name": "Test City",
        }
        pitches = agent.pitch(self._make_brief(), bootstrap_memory(), ctx, "user1")
        assert pitches[0]["claim_kind"] == "neutral"

    def test_pitch_data_contains_day_past_and_day_ahead(self):
        """Pitch data dict carries the split blocks, not the old daily block."""
        from agents.weather.agent import WeatherAgent
        from agents.protocol import bootstrap_memory

        agent = WeatherAgent()
        day_past = {"sunrise": "06:30", "high_f": None, "precipitation_mm_so_far": 0.0}
        day_ahead = {
            "high_f": 75.0, "low_f": 55.0, "hours_remaining": 12,
            "sunset": "19:45",
            "total_precipitation_mm": 0.0, "dominant_condition": "clear", "max_uv": 6.0,
        }
        ctx = {
            "weather_summary": "Currently 22C and clear.",
            "notable_facts": [],
            "location_name": "San Francisco, CA",
            "day_past": day_past,
            "day_ahead": day_ahead,
        }
        pitches = agent.pitch(self._make_brief(), bootstrap_memory(), ctx, "user1")

        data = pitches[0]["data"]
        assert data["day_past"] == day_past
        assert data["day_ahead"] == day_ahead
        assert "daily" not in data

    def test_always_balanced_provenance(self):
        from agents.weather.agent import WeatherAgent
        from agents.protocol import bootstrap_memory

        agent = WeatherAgent()
        ctx = {
            "weather_summary": "Currently 22C and clear.",
            "notable_facts": [],
            "location_name": "Test City",
        }
        pitches = agent.pitch(self._make_brief(), bootstrap_memory(), ctx, "user1")
        assert pitches[0]["provenance_shape"] == "balanced"

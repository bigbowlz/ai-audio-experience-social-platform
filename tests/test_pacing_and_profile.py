"""Tests for the WORDS_PER_MIN pacing knob and Brief.user_profile cold-open thread.

Spec: producer/docs/DESIGN.md §Pacing + §User profile (both 2026-04-18).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import producer
from agents.protocol import Brief, Pitch, UserProfile
from producer import DEFAULT_WORDS_PER_MIN, words_per_min
from producer.events import EventBus, set_default_bus
from producer.script import (
    OPENER_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    _OPENER_DURATION_SEC,
    _target_words,
    _words_to_sec,
    stream_episode_script,
)


# ── WORDS_PER_MIN resolver ────────────────────────────────────────────


class TestWordsPerMin:
    def test_default_is_130(self):
        assert DEFAULT_WORDS_PER_MIN == 130

    def test_resolver_returns_default_when_env_absent(self, monkeypatch):
        monkeypatch.delenv("PRODUCER_WORDS_PER_MIN", raising=False)
        assert words_per_min() == 130

    def test_resolver_reads_env_override(self, monkeypatch):
        monkeypatch.setenv("PRODUCER_WORDS_PER_MIN", "165")
        assert words_per_min() == 165

    def test_resolver_falls_back_on_unparseable(self, monkeypatch):
        monkeypatch.setenv("PRODUCER_WORDS_PER_MIN", "fast")
        assert words_per_min() == DEFAULT_WORDS_PER_MIN

    def test_resolver_falls_back_on_nonpositive(self, monkeypatch):
        monkeypatch.setenv("PRODUCER_WORDS_PER_MIN", "0")
        assert words_per_min() == DEFAULT_WORDS_PER_MIN
        monkeypatch.setenv("PRODUCER_WORDS_PER_MIN", "-5")
        assert words_per_min() == DEFAULT_WORDS_PER_MIN

    def test_resolver_reads_each_call(self, monkeypatch):
        """No import-time capture — env changes reflect immediately."""
        monkeypatch.setenv("PRODUCER_WORDS_PER_MIN", "100")
        assert words_per_min() == 100
        monkeypatch.setenv("PRODUCER_WORDS_PER_MIN", "150")
        assert words_per_min() == 150

    def test_target_words_math(self, monkeypatch):
        monkeypatch.delenv("PRODUCER_WORDS_PER_MIN", raising=False)
        # 60 seconds at 130 wpm = 130 words
        assert _target_words(60) == 130
        # 90 seconds at 130 wpm = 195 words
        assert _target_words(90) == 195
        # env override flows through
        monkeypatch.setenv("PRODUCER_WORDS_PER_MIN", "120")
        assert _target_words(60) == 120

    def test_target_words_floor_one(self, monkeypatch):
        monkeypatch.delenv("PRODUCER_WORDS_PER_MIN", raising=False)
        # Even absurdly small durations return at least 1
        assert _target_words(0) == 1

    def test_words_to_sec_is_inverse(self, monkeypatch):
        monkeypatch.delenv("PRODUCER_WORDS_PER_MIN", raising=False)
        assert _words_to_sec(130) == pytest.approx(60.0)
        assert _words_to_sec(65) == pytest.approx(30.0)


# ── Segment pacing prompt + payload ───────────────────────────────────


class TestPacingPrompt:
    def test_segment_prompt_mentions_target_words(self):
        """Segment system prompt instructs the model on target_words."""
        assert "target_words" in SYSTEM_PROMPT
        assert "Pacing" in SYSTEM_PROMPT or "pacing" in SYSTEM_PROMPT

    def test_opener_prompt_has_pacing_block(self):
        assert "target_words" in OPENER_SYSTEM_PROMPT
        assert "Pacing" in OPENER_SYSTEM_PROMPT

    def test_opener_prompt_addresses_first_name_conditionally(self):
        """Opener prompt must condition address on user_profile.first_name."""
        assert "user_profile" in OPENER_SYSTEM_PROMPT
        assert "first_name" in OPENER_SYSTEM_PROMPT
        # Fallback to "you" must also be named.
        assert '"you"' in OPENER_SYSTEM_PROMPT


# ── pacing_measured event ─────────────────────────────────────────────


def _pitch(agent: str, title: str, seg_len: int = 60) -> Pitch:
    return {
        "agent": agent, "title": title, "hook": "h",
        "source_refs": [], "data": {}, "priority": 0.9,
        "thin_signal": False, "claim_kind": "neutral",
        "provenance_shape": "balanced", "suggested_length_sec": seg_len,
    }


def _brief() -> Brief:
    return {"today_context": {
        "date": "2026-04-17", "day_of_week": "Thursday",
        "time_of_day": "morning", "weather_summary": None,
        "calendar_events": None,
    }}


@pytest.mark.asyncio
async def test_pacing_measured_event_emitted_per_segment(monkeypatch):
    """Each segment emits producer.segment.pacing_measured with measurement fields."""
    monkeypatch.delenv("PRODUCER_WORDS_PER_MIN", raising=False)

    bus = EventBus()
    captured: list[tuple[str, dict]] = []
    bus.subscribe(lambda n, p: captured.append((n, p)))
    set_default_bus(bus)

    try:
        async def fake_generate_segment(segment, brief, is_first, *, previous_segment=None):
            return {
                "agent": segment["agent"],
                "pitch_title": segment["title"],
                "segue_in": "" if is_first else "Moving on,",
                # 130 words ≈ 60 sec @ 130 wpm → near-zero drift for the first segment
                "script": " ".join(["word"] * 130),
                "estimated_length_sec": 60,
            }

        monkeypatch.setattr("producer.script.generate_segment", fake_generate_segment)

        selected = [_pitch("weather", "w", seg_len=60), _pitch("youtube", "y", seg_len=90)]
        async for _ in stream_episode_script(selected, _brief()):
            pass
    finally:
        set_default_bus(EventBus())

    pacing = [(n, p) for n, p in captured if n == "producer.segment.pacing_measured"]
    assert len(pacing) == 2

    first_name, first_payload = pacing[0]
    assert first_payload["agent"] == "weather"
    assert first_payload["target_sec"] == 60
    assert first_payload["target_words"] == 130
    assert first_payload["words"] == 130
    assert first_payload["measured_sec"] == pytest.approx(60.0)
    assert first_payload["drift_sec"] == pytest.approx(0.0)
    assert first_payload["words_per_minute"] == 130
    assert first_payload["estimated_sec_self_report"] == 60

    # Second segment: 90s target, "Moving on," (2 tokens) + 130 script words = 132
    _, second_payload = pacing[1]
    assert second_payload["agent"] == "youtube"
    assert second_payload["target_sec"] == 90
    assert second_payload["target_words"] == 195  # 90s * 130wpm / 60
    assert second_payload["words"] == 132
    # 132 words @ 130 wpm ≈ 60.9s measured, 90s target → drift ≈ -29.1s
    assert second_payload["measured_sec"] == pytest.approx(60.9, abs=0.1)
    assert second_payload["drift_sec"] == pytest.approx(-29.1, abs=0.2)


@pytest.mark.asyncio
async def test_pacing_measured_uses_env_override(monkeypatch):
    """PRODUCER_WORDS_PER_MIN override propagates into pacing event."""
    monkeypatch.setenv("PRODUCER_WORDS_PER_MIN", "100")

    bus = EventBus()
    captured: list[tuple[str, dict]] = []
    bus.subscribe(lambda n, p: captured.append((n, p)))
    set_default_bus(bus)
    try:
        async def fake_generate_segment(segment, brief, is_first, *, previous_segment=None):
            return {
                "agent": segment["agent"],
                "pitch_title": segment["title"],
                "segue_in": "",
                "script": " ".join(["w"] * 100),
                "estimated_length_sec": 60,
            }

        monkeypatch.setattr("producer.script.generate_segment", fake_generate_segment)

        selected = [_pitch("weather", "w", seg_len=60)]
        async for _ in stream_episode_script(selected, _brief()):
            pass
    finally:
        set_default_bus(EventBus())

    pacing = [p for n, p in captured if n == "producer.segment.pacing_measured"]
    assert len(pacing) == 1
    assert pacing[0]["words_per_minute"] == 100
    assert pacing[0]["target_words"] == 100  # 60s * 100wpm / 60 = 100
    assert pacing[0]["measured_sec"] == pytest.approx(60.0)


# ── Brief.user_profile shape + cold-open payload ──────────────────────


class TestUserProfileShape:
    def test_brief_accepts_user_profile(self):
        profile: UserProfile = {"first_name": "Alice", "display_name": "Alice Guesto"}
        brief: Brief = {
            "today_context": {
                "date": "2026-04-17", "day_of_week": "Thursday",
                "time_of_day": "morning", "weather_summary": None,
                "calendar_events": None,
            },
            "user_profile": profile,
        }
        assert brief["user_profile"]["first_name"] == "Alice"

    def test_brief_tolerates_missing_user_profile(self):
        """user_profile is NotRequired — Brief without it is still valid."""
        brief: Brief = {"today_context": {
            "date": "2026-04-17", "day_of_week": "Thursday",
            "time_of_day": "morning", "weather_summary": None,
            "calendar_events": None,
        }}
        assert "user_profile" not in brief

    def test_brief_tolerates_null_user_profile(self):
        """user_profile: None is the orchestrator's explicit 'no auth' signal."""
        brief: Brief = {
            "today_context": {
                "date": "2026-04-17", "day_of_week": "Thursday",
                "time_of_day": "morning", "weather_summary": None,
                "calendar_events": None,
            },
            "user_profile": None,
        }
        assert brief["user_profile"] is None


# ── Orchestrator profile loader ───────────────────────────────────────


class TestOrchestratorProfileLoader:
    def test_loads_profile_when_file_present(self, monkeypatch, tmp_path: Path):
        profile_path = tmp_path / "user_profile.json"
        profile_path.write_text(json.dumps({
            "first_name": "Alice",
            "display_name": "Alice Guesto",
            "email": "patrick@example.com",
        }))
        monkeypatch.setattr("agents.orchestrator._USER_PROFILE_PATH", profile_path)

        from agents.orchestrator import _load_user_profile
        result = _load_user_profile()
        assert result == {"first_name": "Alice", "display_name": "Alice Guesto"}

    def test_returns_none_when_file_absent(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(
            "agents.orchestrator._USER_PROFILE_PATH", tmp_path / "missing.json"
        )
        from agents.orchestrator import _load_user_profile
        assert _load_user_profile() is None

    def test_returns_none_on_parse_error(self, monkeypatch, tmp_path: Path):
        profile_path = tmp_path / "user_profile.json"
        profile_path.write_text("not-json{{")
        monkeypatch.setattr("agents.orchestrator._USER_PROFILE_PATH", profile_path)
        from agents.orchestrator import _load_user_profile
        assert _load_user_profile() is None

    def test_ignores_empty_strings(self, monkeypatch, tmp_path: Path):
        """Empty / whitespace-only names are treated as missing."""
        profile_path = tmp_path / "user_profile.json"
        profile_path.write_text(json.dumps({
            "first_name": "   ",
            "display_name": "",
        }))
        monkeypatch.setattr("agents.orchestrator._USER_PROFILE_PATH", profile_path)
        from agents.orchestrator import _load_user_profile
        assert _load_user_profile() is None

    def test_partial_profile_keeps_present_fields(self, monkeypatch, tmp_path: Path):
        """first_name present, display_name absent → only first_name in result."""
        profile_path = tmp_path / "user_profile.json"
        profile_path.write_text(json.dumps({"first_name": "Alice"}))
        monkeypatch.setattr("agents.orchestrator._USER_PROFILE_PATH", profile_path)
        from agents.orchestrator import _load_user_profile
        result = _load_user_profile()
        assert result == {"first_name": "Alice"}


class TestBriefThreadsUserProfile:
    def test_run_episode_includes_user_profile_in_brief(
        self, monkeypatch, tmp_path: Path
    ):
        """Orchestrator assembles Brief with user_profile from on-disk cache."""
        from unittest.mock import MagicMock

        from agents.orchestrator import run_episode
        from agents.protocol import Pitch, ScopeContext, bootstrap_memory

        profile_path = tmp_path / "user_profile.json"
        profile_path.write_text(json.dumps({
            "first_name": "Alice", "display_name": "Alice Guesto",
        }))
        monkeypatch.setattr("agents.orchestrator._USER_PROFILE_PATH", profile_path)

        stub = MagicMock()
        stub.name = "weather"
        stub.load_memory.return_value = bootstrap_memory()
        stub.fetch_context.return_value = ScopeContext()
        stub.pitch.return_value = [Pitch(
            agent="weather", title="t", hook="h", data={},
            source_refs=[], priority=0.5, thin_signal=False,
            claim_kind="neutral", provenance_shape="balanced",
        )]

        _, brief = run_episode([stub], user_id="test")
        assert brief["user_profile"] == {
            "first_name": "Alice", "display_name": "Alice Guesto",
        }

    def test_run_episode_tolerates_missing_profile_file(
        self, monkeypatch, tmp_path: Path
    ):
        """No profile file → Brief.user_profile is None (not an error)."""
        from unittest.mock import MagicMock

        from agents.orchestrator import run_episode
        from agents.protocol import Pitch, ScopeContext, bootstrap_memory

        monkeypatch.setattr(
            "agents.orchestrator._USER_PROFILE_PATH", tmp_path / "missing.json"
        )

        stub = MagicMock()
        stub.name = "weather"
        stub.load_memory.return_value = bootstrap_memory()
        stub.fetch_context.return_value = ScopeContext()
        stub.pitch.return_value = [Pitch(
            agent="weather", title="t", hook="h", data={},
            source_refs=[], priority=0.5, thin_signal=False,
            claim_kind="neutral", provenance_shape="balanced",
        )]

        _, brief = run_episode([stub], user_id="test")
        assert brief["user_profile"] is None


# ── Opener payload threads user_profile + pacing + fused inputs ───────


def _fake_resp_factory(text: str):
    from types import SimpleNamespace
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


class TestOpenerPayload:
    @pytest.mark.asyncio
    async def test_opener_payload_includes_profile_and_pacing(self, monkeypatch):
        """generate_opener serializes user_profile + target_words into the LLM payload."""
        monkeypatch.delenv("PRODUCER_WORDS_PER_MIN", raising=False)
        captured = {}

        def fake_create(**kwargs):
            captured["messages"] = kwargs["messages"]
            return _fake_resp_factory("x" * 300)

        monkeypatch.setattr(
            "producer.script._client.messages.create", fake_create
        )

        from producer.script import generate_opener
        brief: Brief = {
            "today_context": {
                "date": "2026-04-17", "day_of_week": "Thursday",
                "time_of_day": "morning", "weather_summary": None,
                "calendar_events": None,
            },
            "user_profile": {"first_name": "Alice"},
        }
        weather = _pitch("weather", "Weather", seg_len=45)
        weather["data"] = {"current": {"temp_f": 55, "condition": "rainy"}}
        calendar = _pitch("calendar", "Today", seg_len=30)
        calendar["data"] = {"events": [{"summary": "Standup", "start": "10:00"}]}
        first_content = _pitch("youtube", "Jazz", seg_len=90)

        await generate_opener(weather, calendar, first_content, brief)

        payload = json.loads(captured["messages"][0]["content"])
        assert payload["task"] == "opener"
        assert payload["user_profile"] == {"first_name": "Alice"}
        assert payload["target_words"] == _target_words(_OPENER_DURATION_SEC)
        assert payload["words_per_minute"] == 130
        assert payload["duration_sec_target"] == _OPENER_DURATION_SEC
        # Weather + calendar inputs are shaped, not null.
        assert payload["weather"]["agent"] == "weather"
        assert payload["weather"]["data"] == {
            "current": {"temp_f": 55, "condition": "rainy"},
        }
        assert payload["calendar"]["agent"] == "calendar"
        assert payload["calendar"]["data"]["events"][0]["summary"] == "Standup"
        # first_content_segment carries only the minimal transition handle.
        assert payload["first_content_segment"] == {
            "agent": "youtube", "title": "Jazz", "hook": "h",
        }

    @pytest.mark.asyncio
    async def test_opener_payload_null_profile_when_absent(self, monkeypatch):
        """No user_profile on Brief → payload sends user_profile: null."""
        captured = {}

        def fake_create(**kwargs):
            captured["messages"] = kwargs["messages"]
            return _fake_resp_factory("x" * 300)

        monkeypatch.setattr(
            "producer.script._client.messages.create", fake_create
        )

        from producer.script import generate_opener
        brief: Brief = {
            "today_context": {
                "date": "2026-04-17", "day_of_week": "Thursday",
                "time_of_day": "morning", "weather_summary": None,
                "calendar_events": None,
            },
        }
        await generate_opener(
            _pitch("weather", "w", seg_len=45),
            _pitch("calendar", "c", seg_len=30),
            _pitch("youtube", "y"),
            brief,
        )

        payload = json.loads(captured["messages"][0]["content"])
        assert payload["user_profile"] is None

    @pytest.mark.asyncio
    async def test_opener_payload_nulls_absent_opener_inputs(self, monkeypatch):
        """Missing weather/calendar pass through as null (graceful skip)."""
        captured = {}

        def fake_create(**kwargs):
            captured["messages"] = kwargs["messages"]
            return _fake_resp_factory("x" * 300)

        monkeypatch.setattr(
            "producer.script._client.messages.create", fake_create
        )

        from producer.script import generate_opener
        brief: Brief = {"today_context": {
            "date": "2026-04-17", "day_of_week": "Thursday",
            "time_of_day": "morning", "weather_summary": None,
            "calendar_events": None,
        }}
        await generate_opener(None, None, _pitch("youtube", "y"), brief)

        payload = json.loads(captured["messages"][0]["content"])
        assert payload["weather"] is None
        assert payload["calendar"] is None

    @pytest.mark.asyncio
    async def test_opener_raises_when_too_short(self, monkeypatch):
        """Post-hoc check: opener below _MIN_OPENER_CHARS raises ValueError."""
        def fake_create(**kwargs):
            return _fake_resp_factory("short.")

        monkeypatch.setattr(
            "producer.script._client.messages.create", fake_create
        )

        from producer.script import generate_opener
        brief: Brief = {"today_context": {
            "date": "2026-04-17", "day_of_week": "Thursday",
            "time_of_day": "morning", "weather_summary": None,
            "calendar_events": None,
        }}
        with pytest.raises(ValueError, match="too short"):
            await generate_opener(None, None, _pitch("youtube", "y"), brief)

    @pytest.mark.asyncio
    async def test_opener_raises_when_disable_llm_set(self, monkeypatch):
        monkeypatch.setenv("DISABLE_LLM", "1")
        from producer.script import generate_opener
        brief: Brief = {"today_context": {
            "date": "2026-04-17", "day_of_week": "Thursday",
            "time_of_day": "morning", "weather_summary": None,
            "calendar_events": None,
        }}
        with pytest.raises(RuntimeError, match="DISABLE_LLM"):
            await generate_opener(None, None, _pitch("youtube", "y"), brief)

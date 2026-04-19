"""Per-agent auth preflight tests.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Task 1.2
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from auth import preflight


def test_weather_preflight_noop_when_location_present(tmp_path, monkeypatch):
    location = tmp_path / "weather_location.json"
    location.write_text(json.dumps({"lat": 1.0, "lon": 2.0}))
    monkeypatch.setattr(preflight, "_WEATHER_LOCATION_PATH", location)

    called = mock.Mock()
    monkeypatch.setattr("auth.weather.main", called)
    preflight.ensure_weather_auth()
    assert called.call_count == 0


def test_weather_preflight_triggers_main_when_missing(tmp_path, monkeypatch):
    location = tmp_path / "weather_location.json"
    monkeypatch.setattr(preflight, "_WEATHER_LOCATION_PATH", location)

    def fake_main():
        location.write_text(json.dumps({"lat": 1.0, "lon": 2.0}))

    monkeypatch.setattr("auth.weather.main", fake_main)
    preflight.ensure_weather_auth()  # should not raise
    assert location.exists()


def test_weather_preflight_raises_if_still_missing(tmp_path, monkeypatch):
    location = tmp_path / "weather_location.json"
    monkeypatch.setattr(preflight, "_WEATHER_LOCATION_PATH", location)

    monkeypatch.setattr("auth.weather.main", lambda: None)  # no-op; artifact stays missing
    with pytest.raises(RuntimeError, match="weather auth did not complete"):
        preflight.ensure_weather_auth()


def test_calendar_preflight_noop_when_token_present(tmp_path, monkeypatch):
    token = tmp_path / "calendar_token.json"
    token.write_text("{}")
    monkeypatch.setattr(preflight, "_CALENDAR_TOKEN_PATH", token)

    called = mock.Mock()
    monkeypatch.setattr("auth.calendar_auth.main", called)
    preflight.ensure_calendar_auth()
    assert called.call_count == 0


def test_calendar_preflight_triggers_main_when_missing(tmp_path, monkeypatch):
    token = tmp_path / "calendar_token.json"
    monkeypatch.setattr(preflight, "_CALENDAR_TOKEN_PATH", token)

    def fake_main():
        token.write_text("{}")

    monkeypatch.setattr("auth.calendar_auth.main", fake_main)
    preflight.ensure_calendar_auth()
    assert token.exists()


def test_youtube_preflight_noop_when_probe_dir_populated(tmp_path, monkeypatch):
    probe = tmp_path / "probe_123"
    probe.mkdir()
    # Preflight treats a non-empty dir (specifically: presence of the sentinel)
    # as "probe already captured".
    (probe / "02_subscriptions.json").write_text("[]")
    monkeypatch.setenv("YOUTUBE_PROBE_DIR", str(probe))

    called = mock.Mock()
    monkeypatch.setattr("agents.youtube.capture.oauth_and_capture", called)
    preflight.ensure_youtube_auth()
    assert called.call_count == 0


def test_youtube_preflight_triggers_oauth_and_capture_when_missing(tmp_path, monkeypatch):
    probe = tmp_path / "probe_123"
    monkeypatch.setenv("YOUTUBE_PROBE_DIR", str(probe))

    def fake_oauth_and_capture(out_dir, credentials_path=None):
        # Simulate successful capture writing the minimum expected artifact.
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "02_subscriptions.json").write_text("[]")
        return out

    monkeypatch.setattr(
        "agents.youtube.capture.oauth_and_capture", fake_oauth_and_capture
    )
    preflight.ensure_youtube_auth()
    assert (probe / "02_subscriptions.json").exists()


def test_youtube_preflight_raises_if_capture_produces_nothing(tmp_path, monkeypatch):
    probe = tmp_path / "probe_123"
    monkeypatch.setenv("YOUTUBE_PROBE_DIR", str(probe))
    monkeypatch.setattr(
        "agents.youtube.capture.oauth_and_capture",
        lambda out_dir, credentials_path=None: Path(out_dir),  # no-op; dir stays empty
    )
    with pytest.raises(RuntimeError, match="youtube auth did not complete"):
        preflight.ensure_youtube_auth()


def test_youtube_preflight_rewraps_file_not_found_as_runtime_error(tmp_path, monkeypatch):
    """If oauth_and_capture raises FileNotFoundError (missing credentials),
    preflight rewraps as RuntimeError so the CLI sees a consistent error type."""
    probe = tmp_path / "probe_123"
    monkeypatch.setenv("YOUTUBE_PROBE_DIR", str(probe))

    def raise_fnf(out_dir, credentials_path=None):
        raise FileNotFoundError("client secrets not found at /fake/path")

    monkeypatch.setattr("agents.youtube.capture.oauth_and_capture", raise_fnf)
    with pytest.raises(RuntimeError, match="credentials missing"):
        preflight.ensure_youtube_auth()


def test_ensure_agent_auth_unknown_name_raises_value_error():
    """Task 1.3 calls this by user-supplied name; unknown names must surface as
    ValueError, not leak KeyError from the internal dict lookup."""
    with pytest.raises(ValueError, match="no preflight registered for agent 'bogus'"):
        preflight.ensure_agent_auth("bogus")

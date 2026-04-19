"""Per-agent auth preflight for the v0 CLI pivot.

Each activated internal agent gets one preflight call before instantiation.
Contract per helper:
  - Artifact present → return silently.
  - Artifact missing → run the inline auth flow; on return, re-check.
  - Artifact still missing after auth → raise RuntimeError with a clear next step.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Phase 1
"""
from __future__ import annotations

import os
from pathlib import Path

_CONFIG_DIR = Path.home() / ".config" / "radio-podcast"
_WEATHER_LOCATION_PATH = _CONFIG_DIR / "weather_location.json"
_CALENDAR_TOKEN_PATH = _CONFIG_DIR / "calendar_token.json"

_DEFAULT_YOUTUBE_PROBE_DIR = Path("tmp") / "ydata" / "probe_1776208130"
# Probe is considered "captured" when this file exists in the probe dir.
# YouTubeAgent reads this file at agents/youtube/agent.py:68.
_YOUTUBE_PROBE_SENTINEL = "02_subscriptions.json"


def ensure_weather_auth() -> None:
    """Ensure weather_location.json exists; trigger browser geolocation if not."""
    if _WEATHER_LOCATION_PATH.exists():
        return
    print(
        f"[preflight] weather: {_WEATHER_LOCATION_PATH.name} missing — "
        f"launching browser geolocation flow …"
    )
    import auth.weather as weather_auth
    weather_auth.main()
    if not _WEATHER_LOCATION_PATH.exists():
        raise RuntimeError(
            "weather auth did not complete — "
            f"{_WEATHER_LOCATION_PATH} still missing. "
            "Re-run `python -m auth.weather` manually to debug."
        )


def ensure_calendar_auth() -> None:
    """Ensure calendar_token.json exists; trigger Google OAuth if not."""
    if _CALENDAR_TOKEN_PATH.exists():
        return
    print(
        f"[preflight] calendar: {_CALENDAR_TOKEN_PATH.name} missing — "
        f"launching Google OAuth flow …"
    )
    import auth.calendar_auth as calendar_auth
    calendar_auth.main()
    if not _CALENDAR_TOKEN_PATH.exists():
        raise RuntimeError(
            "calendar auth did not complete — "
            f"{_CALENDAR_TOKEN_PATH} still missing. "
            "Re-run `python auth/calendar_auth.py` manually to debug."
        )


def ensure_youtube_auth() -> None:
    """Ensure the YouTube probe dir is populated; run live OAuth + capture if not.

    Triggers `agents.youtube.capture.oauth_and_capture` against the same
    dir that YouTubeAgent will read from (YOUTUBE_PROBE_DIR, or the
    default at tmp/ydata/probe_1776208130). The sentinel file
    (02_subscriptions.json) is what `_load_probe_data` opens first at
    agents/youtube/agent.py:68 — its absence is our "not yet captured"
    signal.
    """
    probe_dir = Path(
        os.environ.get("YOUTUBE_PROBE_DIR", str(_DEFAULT_YOUTUBE_PROBE_DIR))
    )
    sentinel = probe_dir / _YOUTUBE_PROBE_SENTINEL
    if sentinel.exists():
        return
    print(
        f"[preflight] youtube: probe not captured ({sentinel} missing) — "
        f"launching YouTube OAuth + capture into {probe_dir} …"
    )
    import agents.youtube.capture as yt_capture
    yt_capture.oauth_and_capture(out_dir=probe_dir)
    if not sentinel.exists():
        raise RuntimeError(
            "youtube auth did not complete — "
            f"{sentinel} still missing after capture. "
            f"Re-run `python -m agents.youtube.capture --out {probe_dir}` "
            "manually to debug."
        )


_PREFLIGHT_BY_NAME = {
    "weather": ensure_weather_auth,
    "calendar": ensure_calendar_auth,
    "youtube": ensure_youtube_auth,
}


def ensure_agent_auth(name: str) -> None:
    """Dispatch preflight by agent name (the same names used in orchestrator)."""
    try:
        fn = _PREFLIGHT_BY_NAME[name]
    except KeyError as e:
        raise ValueError(f"no preflight registered for agent {name!r}") from e
    fn()

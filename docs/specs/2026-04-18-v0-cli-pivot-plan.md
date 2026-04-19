# v0 CLI Pivot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hardcoded internal-agent list in `agents/orchestrator.py:226` with flag-driven activation (`--weather`, `--calendar`, `--youtube`), per-agent auth preflight that triggers the required auth flow inline when missing, and a CLI-native playback UX (auto-play via `afplay` + hotkey feedback that feeds ProducerMemory between runs to show learning on the demo). Frontend webpage selection and the web player are deferred to v1.

**Architecture:** Four phases, each independently shippable.

- **Phase 0 — Flag-driven agent selection.** Refactor the monolithic `if __name__ == "__main__":` block at `agents/orchestrator.py:187-369` into a testable `cli_main(argv)` function. Add `--weather`, `--calendar`, `--youtube` boolean flags. Build `internal_agents` conditionally. Zero agent flags → `parser.error(...)` with a clear message. `--no-external` semantics are preserved untouched (external is Producer-invoked from the marketplace, not pre-selected by the user; flipping it to opt-in is out of scope — see §Non-goals).
- **Phase 1 — Auth preflight.** New `auth/preflight.py` with one helper per selectable agent. For each activated agent, the CLI calls its preflight *before* instantiation: if the required artifact is present, continue; if missing, run the inline auth flow (weather geolocation, Google Calendar OAuth, or YouTube Data API OAuth + capture). The "Claude-Code-login-style" UX — auto-open browser **and** print the authorization URL to stdout so the user can click it manually if the auto-open fails — is inherited natively: `google_auth_oauthlib.flow.InstalledAppFlow.run_local_server()` prints the URL by default (used by calendar + youtube); `auth/weather.py:147` already prints `Opening browser: {url}` before calling `webbrowser.open()`. No per-flow UX rework required — just confirm via smoke test.
- **Phase 2 — Playback + feedback (learning demo).** Add a macOS-only CLI player that auto-plays the generated episode via `afplay` subprocess and reads single-key hotkeys from `termios` raw stdin: `l` (like · 1.10x), `s` (skip · 0.90x), `r` (repeat · 1.20x), `p` / space (pause — NOT a signal), `q` (quit). Feedback signals append to `~/.config/radio-podcast/feedback.jsonl`. On next `cli_main` invocation, `learning_loop/seed_from_feedback.py` reads that log, computes per-agent weight products (clamped to `[AGENT_WEIGHT_MIN=0.3, AGENT_WEIGHT_MAX=2.0]` from `producer/memory.py:31-33`), and seeds `producer_memory` via `learning_loop.seed_producer_memory` — the exact demo seam sanctioned by `learning_loop/docs/DESIGN.md:v0 stub contract`. End-of-episode output prints the computed deltas so a demonstrator can say "watch how episode 2 re-orders the lineup."
- **Phase 3 — Docs sync.** README usage block rewrites around the new CLI surface; TODOS.md moves webpage selection + web player into v1. Confirms there are no frontend artifacts to delete (survey 2026-04-18: none detected).

**Tech Stack:** Python 3.12+, stdlib `argparse`, `pathlib`, `subprocess`, `termios`, `tty`, `sys`, `asyncio`, `pytest`, `pytest-monkeypatch`. **macOS-only dependency:** `/usr/bin/afplay` (ships with macOS). No new third-party deps.

**Memory references applied:**

- `project_v0_cli_pivot.md` — absent flag = skip that agent; zero flags is an error, not an empty show. CLI is entirely v0; v1 is frontend-only, so no cross-platform CLI concerns.
- `agentic_payment_pivot.md` — external/Alices round stays intact; it's the demo's money shot.
- `feedback_producer_memory_deterministic.md` — weight computation from the feedback log stays a pure function; `SIGNAL_MULTIPLIERS` are applied multiplicatively with no LLM involvement.
- `feedback_component_by_component_dev.md` — YouTube agent is the active component; this pivot is cross-cutting but does not alter YouTube's internals.
- `multi_agent_marketplace_rationale.md` — feedback log bumps only `ProducerMemory.agent_weights` (inter-agent). Intra-agent topic signals stay out of scope for v0.
- `feedback_read_docs_before_asking.md` — every task cites the code line it modifies.

---

## Non-goals (out of scope for v0)

1. **Frontend / webpage selection UI and web player.** Deferred to v1. The CLI player in Phase 2 is explicitly v0-only — the entire CLI experience (selection + auth + playback + feedback) is disposable once v1 ships.
2. **Cross-platform CLI (Linux / Windows playback).** v0 uses `afplay`, macOS-only. Linux/Windows support is NOT a follow-up in v1 either; v1 replaces the CLI outright with the frontend.
3. **Changing `--no-external` into an opt-in `--external` flag.** External is Producer-invoked from the marketplace; the user doesn't pre-select it. Keeping today's default-on behavior means zero churn for the `payment.*`, `producer.external_decision.*`, and `producer.marketplace.*` event paths wired in the 2026-04-17 producer-alignment plan.
4. **Refactoring the two-round pitch flow, Brief assembly, or producer wiring.** The orchestrator already handles an arbitrary `internal_agents: list[DataAgent]` — no changes required below the CLI layer.
5. **Intra-agent (topic-level) learning from feedback.** Phase 2 only updates `ProducerMemory.agent_weights` (inter-agent). `AgentMemory.topic_multiplier` writes stay stubbed per `learning_loop/docs/DESIGN.md`. Pulling that forward would require per-agent signal routing rules and is out of scope.
6. **Streaming playback (segment 0 plays while segment N is still generating).** Phase 2 plays post-generation (all segments finish, then playback starts). This trades some "snappy demo feel" for a ~40% smaller async surface; if the 30-60s TTS wait hurts the demo, the streaming variant is an opt-in upgrade (route `on_segment_done` through `run_episode_pipeline` into an `asyncio.Queue`). Not in this plan.
7. **Pause as a learning signal.** Pause (`p` / space) is pure playback control — it does NOT write to the feedback log. Only `l` / `s` / `r` are signals. Locked 2026-04-18.

---

## File structure

| Path                              | Phase | Action     | Responsibility                                                                                       |
| --------------------------------- | ----- | ---------- | ---------------------------------------------------------------------------------------------------- |
| `agents/orchestrator.py`              | 0,1,2 | modify     | Extract `cli_main(argv)`; add agent flags; call preflight; call `hydrate_producer_memory`; run pipeline; await `play_episode`; print weight-delta summary |
| `auth/preflight.py`                   | 1     | **create** | `ensure_weather_auth`, `ensure_calendar_auth`, `ensure_youtube_auth`                                 |
| `agents/youtube/capture.py`           | 1     | modify     | Extract `oauth_and_capture(out_dir, credentials_path)` helper from `main()` for programmatic reuse   |
| `auth/weather.py`                     | 1     | verify     | Confirm `def main() -> None` already exists at line 136 (no change expected)                         |
| `player/__init__.py`                  | 2     | **create** | Module marker (player/ is docs-only today)                                                           |
| `player/playback.py`                  | 2     | **create** | `AfplaySession(path)` — subprocess wrapper with `start`/`wait`/`stop`/`pause`/`resume`/`restart` via SIGTERM/SIGSTOP/SIGCONT |
| `player/hotkeys.py`                   | 2     | **create** | `raw_key_reader()` — termios context manager yielding single keypresses from stdin                   |
| `player/cli_player.py`                | 2     | **create** | `async def play_episode(segments, user_id, episode_id, on_feedback)` — plays sequentially, dispatches hotkey → action (like/skip/repeat/pause/quit) |
| `learning_loop/feedback_log.py`       | 2     | **create** | `log_signal(record)` (append JSONL); `iter_signals(user_id)` (read, skip malformed)                  |
| `learning_loop/seed_from_feedback.py` | 2     | **create** | `hydrate_producer_memory(user_id) -> dict[str, float]` — product-of-multipliers per agent, clamped, calls `seed_producer_memory` |
| `learning_loop/__init__.py`           | 2     | modify     | Re-export `seed_producer_memory`, `feedback_log.log_signal`, `seed_from_feedback.hydrate_producer_memory` |
| `tests/test_orchestrator_cli.py`      | 0,1   | **create** | Flag parsing → expected `internal_agents`; zero flags errors; preflight called once per active agent |
| `tests/test_auth_preflight.py`        | 1     | **create** | Artifact present → no-op; artifact missing → auth `main()` called; still missing → `RuntimeError`    |
| `tests/test_playback.py`              | 2     | **create** | `AfplaySession` lifecycle under `subprocess.Popen` stub; pause/resume sends SIGSTOP/SIGCONT          |
| `tests/test_hotkeys.py`               | 2     | **create** | Fake stdin yields correct `KeyPress` values; termios raw mode entered/exited                         |
| `tests/test_cli_player.py`            | 2     | **create** | Keypress `l`/`s`/`r`/`p`/`q` → correct action + feedback callback; pause does NOT call `on_feedback` |
| `tests/test_feedback_log.py`          | 2     | **create** | Append + iter round-trip; malformed line skipped; filters by user_id                                 |
| `tests/test_seed_from_feedback.py`    | 2     | **create** | `l l s` on agent = `1.10 * 1.10 * 0.90`; clamp at 0.3 / 2.0; empty log → `{}`                       |
| `README.md`                           | 3     | modify     | Replace any frontend-era usage with CLI invocation; document each flag and its auth prereq; document hotkeys |
| `TODOS.md`                            | 3     | modify     | Move webpage selection + web player / frontend work under an explicit "v1" heading                   |
| `CLAUDE.md`                           | 3     | modify     | One-line note: v0 is CLI-only; frontend deferred to v1                                               |

---

## Phase 0 — Flag-driven agent selection

Pre-condition: clean working tree in `agents/orchestrator.py` and `tests/`. Each task is one commit.

### Task 0.1: Extract `cli_main(argv)` from the `__main__` block

Makes the CLI testable without refactoring behavior. Pure mechanical move.

**Files:**

- Modify: `agents/orchestrator.py:187-369`

- [ ] **Step 1: Read the current `__main__` block end-to-end**

Run: `sed -n '187,370p' agents/orchestrator.py`
Note the order: subscribe sink → argparse → env flag → banner → instantiate agents → `run_episode` → external flow → print pitches → memory/segments/bonus → script or full pipeline or fallback JSON.

- [ ] **Step 2: Wrap the entire block body in a function**

Replace `agents/orchestrator.py:187-369` with:

```python
def cli_main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns process exit code.

    argv=None → argparse reads sys.argv. Passing argv enables unit tests
    without monkeypatching sys.argv.
    """
    import argparse

    from payment.stub import initiate_tx
    from producer.events import JsonlSink, emit, subscribe
    from producer.external import (
        decide_external_invocation,
        query_marketplace,
        select_external,
    )

    subscribe(JsonlSink())

    parser = argparse.ArgumentParser(
        description="Run one episode generation pass and print EpisodeScript JSON."
    )
    parser.add_argument("--user-id", default="dev")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument(
        "--no-external",
        action="store_true",
        help="Skip external pitch round (Phase 2 escape hatch)",
    )
    args = parser.parse_args(argv)

    # ── (body of the original __main__ block, unchanged) ──
    # … every existing line from `if args.no_llm:` through the final print …

    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
```

Keep every behavior byte-identical in this step — no flags added yet.

- [ ] **Step 3: Run existing tests to prove no regression**

Run: `pytest tests/test_agents_orchestrator.py -v`
Expected: PASS (same count as before).

- [ ] **Step 4: Commit**

```bash
git add agents/orchestrator.py
git commit -m "refactor(orchestrator): extract cli_main(argv) from __main__ block"
```

### Task 0.2: Add `--weather`, `--calendar`, `--youtube` flags and drive agent list

Implements the core pivot. Zero flags is a `parser.error`, not a silent empty show.

**Files:**

- Modify: `agents/orchestrator.py` (the `cli_main` added in Task 0.1)
- Create: `tests/test_orchestrator_cli.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_orchestrator_cli.py`:

```python
"""CLI flag parsing tests for the v0 CLI pivot.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Task 0.2
"""
from __future__ import annotations

import pytest

from agents.orchestrator import _select_internal_agent_classes


def test_weather_flag_selects_weather_only():
    names = _select_internal_agent_classes(
        weather=True, calendar=False, youtube=False
    )
    assert names == ["weather"]


def test_all_three_flags_selects_all_three_in_fixed_order():
    names = _select_internal_agent_classes(
        weather=True, calendar=True, youtube=True
    )
    # Fixed order: weather, calendar, youtube (matches current
    # hardcoded list at agents/orchestrator.py:226 pre-pivot).
    assert names == ["weather", "calendar", "youtube"]


def test_zero_flags_raises_systemexit():
    with pytest.raises(SystemExit):
        _select_internal_agent_classes(
            weather=False, calendar=False, youtube=False
        )


def test_calendar_plus_youtube_skips_weather():
    names = _select_internal_agent_classes(
        weather=False, calendar=True, youtube=True
    )
    assert names == ["calendar", "youtube"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_orchestrator_cli.py -v`
Expected: FAIL with `ImportError: cannot import name '_select_internal_agent_classes'`.

- [ ] **Step 3: Add the selector helper and the argparse flags**

In `agents/orchestrator.py`, add this module-level helper near `_load_user_profile` (above `run_episode`):

```python
# ── CLI helpers ──────────────────────────────────────────────────────

_INTERNAL_AGENT_ORDER = ("weather", "calendar", "youtube")


def _select_internal_agent_classes(
    *, weather: bool, calendar: bool, youtube: bool
) -> list[str]:
    """Return the list of internal agent *names* activated by CLI flags.

    Order is fixed (weather → calendar → youtube) to keep SSE/event
    ordering deterministic across runs and to match the pre-pivot
    hardcoded order. Zero flags raises SystemExit via parser.error
    semantics — the CLI should print a helpful message.
    """
    names = [
        n for n, on in zip(
            _INTERNAL_AGENT_ORDER, (weather, calendar, youtube), strict=True,
        ) if on
    ]
    if not names:
        raise SystemExit(
            "Select at least one agent: --weather, --calendar, --youtube "
            "(use --help for details)."
        )
    return names
```

Then inside `cli_main`, add three flags to the parser:

```python
parser.add_argument(
    "--weather", action="store_true",
    help="Activate the Weather agent (requires weather_location.json; "
         "auth flow runs automatically if missing)",
)
parser.add_argument(
    "--calendar", action="store_true",
    help="Activate the Calendar agent (requires Google OAuth; "
         "auth flow runs automatically if missing)",
)
parser.add_argument(
    "--youtube", action="store_true",
    help="Activate the YouTube agent (v0: requires a probe dir at "
         "$YOUTUBE_PROBE_DIR or tmp/ydata/probe_1776208130)",
)
```

Replace the hardcoded list at the old `agents/orchestrator.py:226`:

```python
# OLD (delete):
#   internal_agents = [WeatherAgent(), CalendarAgent(), YouTubeAgent()]

# NEW:
agent_names = _select_internal_agent_classes(
    weather=args.weather,
    calendar=args.calendar,
    youtube=args.youtube,
)
from agents.calendar.agent import CalendarAgent
from agents.weather.agent import WeatherAgent
from agents.youtube.agent import YouTubeAgent

_CLASS_BY_NAME = {
    "weather": WeatherAgent,
    "calendar": CalendarAgent,
    "youtube": YouTubeAgent,
}
internal_agents = [_CLASS_BY_NAME[n]() for n in agent_names]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_orchestrator_cli.py -v`
Expected: PASS (4/4).

- [ ] **Step 5: Verify existing orchestrator tests still pass**

Run: `pytest tests/test_agents_orchestrator.py -v`
Expected: PASS. If any test relied on the hardcoded list in `cli_main`, update the test to pass explicit `argv=["--weather", "--calendar", "--youtube", "--no-llm"]`.

- [ ] **Step 6: Commit**

```bash
git add agents/orchestrator.py tests/test_orchestrator_cli.py
git commit -m "feat(orchestrator): flag-driven internal agent activation (v0 CLI pivot)"
```

### Task 0.3: Smoke test the CLI with one flag

Confirms end-to-end that a single-agent show runs without touching auth yet (weather has no OAuth — easiest path).

**Files:**

- No code changes; read-only verification.

- [ ] **Step 1: Run the CLI with only `--weather --no-llm`**

Run: `python -m agents.orchestrator --weather --no-llm`
Expected (stdout): banner → weather pitch(es) printed → segment JSON printed; exit 0.
**If** `~/.config/radio-podcast/weather_location.json` is missing, expected behavior **today** is a crash inside `WeatherAgent.fetch_context`. That's the exact gap Phase 1 closes — do not fix it here.

- [ ] **Step 2: Run the CLI with zero flags**

Run: `python -m agents.orchestrator --no-llm`
Expected: exits non-zero with `"Select at least one agent: --weather, --calendar, --youtube …"`.

- [ ] **Step 3: Commit (docs only, if anything changed)**

No commit expected from Task 0.3 — verification only.

---

## Phase 1 — Auth preflight

Each activated agent gets a preflight call *before* its constructor runs. Preflight is the single hook that turns "flag was set" into "agent is actually usable."

### Task 1.1: Extract `oauth_and_capture(out_dir, credentials_path)` in `agents/youtube/capture.py`

`main()` in `agents/youtube/capture.py:173-194` tightly couples argparse to the OAuth + capture flow. Preflight needs the same flow with a programmatic `out_dir` (pointing at `YOUTUBE_PROBE_DIR` — what `YouTubeAgent` actually reads at `agents/youtube/agent.py:48`) without monkeying with `sys.argv`. Minimal refactor: lift the three-line OAuth body into a named helper, then have `main()` call it. `auth/weather.py:136` already has `def main() -> None` — no change needed there, just confirm it.

**Files:**

- Modify: `agents/youtube/capture.py`

- [ ] **Step 1: Confirm `auth/weather.py:main` already exists**

Run: `grep -n "^def main" auth/weather.py`
Expected: one hit at line 136 (`def main() -> None:`). No edit required.

- [ ] **Step 2: Add `oauth_and_capture` helper in `agents/youtube/capture.py`**

Replace `agents/youtube/capture.py:171-194` (the `# ── Standalone entry point ──` section through `main()`) with:

```python
# ── OAuth + capture entry points ─────────────────────────────────────

def oauth_and_capture(
    out_dir: Path | None = None,
    credentials_path: Path | None = None,
) -> Path:
    """Run the YouTube Data API OAuth flow and capture probe data.

    Opens the browser for consent (and prints the authorization URL to
    stdout as a fallback), fetches subscriptions/likes/topicDetails via
    youtube.readonly, and writes the probe JSON files to out_dir.

    Reused by preflight (auth/preflight.py::ensure_youtube_auth) and the
    standalone CLI (main()). Keeps the three-line OAuth boilerplate in
    one place.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: PLC0415

    out = Path(out_dir) if out_dir is not None else _DEFAULT_OUT
    creds_file = Path(credentials_path) if credentials_path is not None else _CREDENTIALS
    if not creds_file.exists():
        raise FileNotFoundError(
            f"YouTube OAuth client secrets not found: {creds_file}. "
            "Set YOUTUBE_OAUTH_CLIENT_SECRET or place credentials at "
            f"{_CREDENTIALS}."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), _SCOPES)
    creds = flow.run_local_server(port=0)
    session = AuthorizedSession(creds)
    return capture(session, out)


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture YouTube signals via OAuth")
    parser.add_argument(
        "--out", type=Path, default=_DEFAULT_OUT,
        help=f"Output directory (default: {_DEFAULT_OUT})",
    )
    parser.add_argument(
        "--credentials", type=Path, default=_CREDENTIALS,
        help=f"OAuth desktop client secrets JSON (default: {_CREDENTIALS})",
    )
    args = parser.parse_args()
    oauth_and_capture(out_dir=args.out, credentials_path=args.credentials)


if __name__ == "__main__":
    main()
```

Public API surface added: `oauth_and_capture(out_dir, credentials_path)`. `main()` behavior is unchanged end-to-end.

- [ ] **Step 3: Verify standalone capture still works (dry-run)**

Run: `python -m agents.youtube.capture --help`
Expected: argparse help output with `--out` and `--credentials` flags; exit 0. **Do not** run a full live capture in this step — that triggers a real OAuth popup.

- [ ] **Step 4: Commit**

```bash
git add agents/youtube/capture.py
git commit -m "refactor(youtube): expose oauth_and_capture() helper for programmatic preflight"
```

### Task 1.2: Create `auth/preflight.py` with per-agent helpers

**Files:**

- Create: `auth/preflight.py`
- Create: `tests/test_auth_preflight.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_auth_preflight.py`:

```python
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
    # Preflight treats a non-empty dir as "probe already captured".
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_auth_preflight.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'auth.preflight'`.

- [ ] **Step 3: Create `auth/preflight.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_auth_preflight.py -v`
Expected: PASS (7/7).

- [ ] **Step 5: Commit**

```bash
git add auth/preflight.py tests/test_auth_preflight.py
git commit -m "feat(auth): add per-agent preflight with inline auth trigger"
```

### Task 1.3: Wire preflight into `cli_main`

**Files:**

- Modify: `agents/orchestrator.py` (`cli_main`)
- Modify: `tests/test_orchestrator_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_orchestrator_cli.py`:

```python
def test_cli_main_calls_preflight_once_per_active_agent(monkeypatch):
    """Every activated agent triggers its preflight exactly once, before
    the agent is instantiated."""
    from unittest import mock
    import agents.orchestrator as orch

    calls: list[str] = []
    monkeypatch.setattr(
        "auth.preflight.ensure_agent_auth",
        lambda name: calls.append(name),
    )

    # Stub the heavy path: replace run_episode + everything after it.
    monkeypatch.setattr(orch, "run_episode", lambda *a, **k: ({}, {"today_context": {}, "user_profile": None}))
    # Also stub subscribe/JsonlSink so we don't spam stdout.
    monkeypatch.setattr("producer.events.subscribe", lambda *_a, **_k: None)

    with pytest.raises(SystemExit):
        orch.cli_main(["--weather", "--calendar", "--no-llm", "--no-external"])
    # SystemExit is acceptable here because downstream (producer memory etc.)
    # is stubbed; we only assert the preflight ordering.
    assert calls == ["weather", "calendar"]
```

> **Reviewer note:** This test is deliberately loose — it asserts the *set and order* of preflight calls, not full-pipeline completion. Downstream modules (producer.memory, producer.bonus, producer.script, pipeline) are exercised in existing `tests/test_pipeline.py` etc. If the assertion style above creates a brittle coupling, tighten or relax per local test-style norms — the invariant to preserve is "one preflight per active agent, before instantiation."

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_orchestrator_cli.py::test_cli_main_calls_preflight_once_per_active_agent -v`
Expected: FAIL (either missing `ensure_agent_auth` import or `calls == []`).

- [ ] **Step 3: Wire preflight into `cli_main`**

In `agents/orchestrator.py`, within `cli_main`, replace the block added in Task 0.2 (the one starting `agent_names = _select_internal_agent_classes(...)` through `internal_agents = [_CLASS_BY_NAME[n]() for n in agent_names]`) with this full block:

```python
agent_names = _select_internal_agent_classes(
    weather=args.weather,
    calendar=args.calendar,
    youtube=args.youtube,
)

from auth.preflight import ensure_agent_auth
for name in agent_names:
    ensure_agent_auth(name)

from agents.calendar.agent import CalendarAgent
from agents.weather.agent import WeatherAgent
from agents.youtube.agent import YouTubeAgent

_CLASS_BY_NAME = {
    "weather": WeatherAgent,
    "calendar": CalendarAgent,
    "youtube": YouTubeAgent,
}
internal_agents = [_CLASS_BY_NAME[n]() for n in agent_names]
```

Ordering matters: preflight runs *before* the agent class is imported so that a missing artifact surfaces as a clean preflight error, not a constructor explosion.

- [ ] **Step 4: Run the full test suite**

Run: `pytest tests/test_orchestrator_cli.py tests/test_auth_preflight.py tests/test_agents_orchestrator.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/orchestrator.py tests/test_orchestrator_cli.py
git commit -m "feat(orchestrator): run auth preflight before instantiating each agent"
```

### Task 1.4: End-to-end smoke test

**Files:**

- No code changes; read-only verification.

- [ ] **Step 1: With all artifacts present, run the full path**

Run: `python -m agents.orchestrator --weather --calendar --youtube --no-llm`
Expected: preflight silent (no "launching browser" messages); pitches from all three agents; segment JSON printed.

- [ ] **Step 2: With one artifact missing, confirm auth is triggered**

```bash
mv ~/.config/radio-podcast/weather_location.json /tmp/weather_location.bak
python -m agents.orchestrator --weather --no-llm   # should open browser
# After approving location in browser:
mv /tmp/weather_location.bak ~/.config/radio-podcast/weather_location.json   # restore
```

Expected: `[preflight] weather: weather_location.json missing — launching browser geolocation flow …` appears, browser opens, run continues after approval.

- [ ] **Step 3: With no probe captured yet, confirm `--youtube` launches YouTube OAuth**

```bash
TMP=$(mktemp -d)
YOUTUBE_PROBE_DIR="$TMP/probe_demo" python -m agents.orchestrator --youtube --no-llm
```

Expected (stdout, in order):
1. `[preflight] youtube: probe not captured (…/02_subscriptions.json missing) — launching YouTube OAuth + capture into …`
2. Browser auto-opens to Google's consent screen **and** the authorization URL is printed to stdout (that's `InstalledAppFlow.run_local_server` built-in behavior — the fallback link if the browser auto-open fails).
3. After consent: `Done. Files written to <probe dir>`.
4. YouTube agent pitches print.

If the browser doesn't open automatically, the printed URL is the manual fallback — copy it into a browser and authenticate. This is the same UX Claude Code uses for subscription login.

- [ ] **Step 4: Confirm fallback-URL UX for calendar and weather as well**

For calendar (`--calendar` with token absent): `InstalledAppFlow.run_local_server` prints the consent URL before `webbrowser.open()`. Copy-clickable in every modern terminal.

For weather (`--weather` with location absent): `auth/weather.py:147` prints `Opening browser: http://127.0.0.1:<port>/`. Same pattern.

No code change required; this step is just verifying stdout shows the URL prominently in all three flows.

---

## Phase 2 — Playback + feedback (learning demo)

### Hotkey reference

| Key             | Action                  | Learning signal       | Playback effect                                  |
| --------------- | ----------------------- | --------------------- | ------------------------------------------------ |
| `l`             | Like current segment    | `like` (1.10×)        | Continue playing                                 |
| `s`             | Skip to next segment    | `skip` (0.90×)        | SIGTERM afplay, advance to next segment          |
| `r`             | Repeat current segment  | `replay` (1.20×)      | SIGTERM afplay, restart same segment             |
| `p` / space     | Pause / resume          | **none** (UX only)    | Toggle SIGSTOP ↔ SIGCONT on afplay               |
| `q`             | Quit episode            | none                  | SIGTERM afplay, exit player cleanly              |
| any other key   | (no-op)                 | none                  | Ignored                                          |

Multiple presses of the same learning key on the same segment are **additive** — `l l` writes two `like` records, yielding 1.10 × 1.10 = 1.21× on the agent's weight (clamped at 2.0). No deduplication.

Each learning-signal keypress results in one JSONL record appended to `~/.config/radio-podcast/feedback.jsonl`:

```json
{"user_id": "dev", "episode_id": "ep-1776273600", "segment_index": 2, "agent": "weather", "pitch_title": "Morning: cold front moving in", "signal": "like", "ts": "2026-04-18T13:42:17+00:00"}
```

### Task 2.1: `AfplaySession` subprocess wrapper

Sync class — pure subprocess management. No stdin, no asyncio. Used from async code via `asyncio.to_thread`.

**Files:**

- Create: `player/__init__.py`
- Create: `player/playback.py`
- Create: `tests/test_playback.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_playback.py`:

```python
"""AfplaySession subprocess lifecycle tests.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Task 2.1
"""
from __future__ import annotations

import signal
from unittest import mock

import pytest

from player.playback import AfplaySession


def _fake_popen(rc_on_wait: int = 0) -> mock.Mock:
    proc = mock.Mock()
    proc.pid = 4242
    proc.returncode = None

    def _wait(timeout=None):
        proc.returncode = rc_on_wait
        return rc_on_wait

    proc.wait.side_effect = _wait
    proc.send_signal = mock.Mock()
    proc.terminate = mock.Mock()
    return proc


def test_start_spawns_afplay_with_path(monkeypatch):
    fake_proc = _fake_popen()
    popen = mock.Mock(return_value=fake_proc)
    monkeypatch.setattr("player.playback.subprocess.Popen", popen)

    session = AfplaySession("/tmp/seg0.mp3")
    session.start()

    popen.assert_called_once()
    args, _ = popen.call_args
    assert args[0] == ["/usr/bin/afplay", "/tmp/seg0.mp3"]


def test_pause_sends_sigstop_then_resume_sends_sigcont(monkeypatch):
    fake_proc = _fake_popen()
    monkeypatch.setattr(
        "player.playback.subprocess.Popen",
        mock.Mock(return_value=fake_proc),
    )

    session = AfplaySession("/tmp/seg0.mp3")
    session.start()
    session.pause()
    fake_proc.send_signal.assert_called_with(signal.SIGSTOP)
    assert session.is_paused is True

    session.resume()
    fake_proc.send_signal.assert_called_with(signal.SIGCONT)
    assert session.is_paused is False


def test_stop_terminates(monkeypatch):
    fake_proc = _fake_popen()
    monkeypatch.setattr(
        "player.playback.subprocess.Popen",
        mock.Mock(return_value=fake_proc),
    )

    session = AfplaySession("/tmp/seg0.mp3")
    session.start()
    session.stop()
    fake_proc.terminate.assert_called_once()


def test_wait_blocks_until_proc_exits(monkeypatch):
    fake_proc = _fake_popen(rc_on_wait=0)
    monkeypatch.setattr(
        "player.playback.subprocess.Popen",
        mock.Mock(return_value=fake_proc),
    )

    session = AfplaySession("/tmp/seg0.mp3")
    session.start()
    rc = session.wait()
    assert rc == 0
    fake_proc.wait.assert_called_once()


def test_start_raises_if_afplay_missing(monkeypatch):
    monkeypatch.setattr("player.playback.AFPLAY_PATH", "/nonexistent/afplay")
    monkeypatch.setattr("pathlib.Path.exists", lambda self: False)
    session = AfplaySession("/tmp/seg0.mp3")
    with pytest.raises(RuntimeError, match="afplay not found"):
        session.start()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_playback.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'player'`.

- [ ] **Step 3: Create `player/__init__.py` (empty) and `player/playback.py`**

`player/__init__.py`:

```python
"""v0 CLI player — macOS-only (afplay). Deferred in v1 (frontend replaces this).

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Phase 2
"""
```

`player/playback.py`:

```python
"""afplay subprocess wrapper — sync, macOS-only.

Used from async code via asyncio.to_thread to avoid blocking the event loop.
SIGSTOP / SIGCONT give us real pause/resume (afplay has no built-in pause).

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Task 2.1
"""
from __future__ import annotations

import signal
import subprocess
from pathlib import Path

AFPLAY_PATH = "/usr/bin/afplay"


class AfplaySession:
    """One playback session for one audio file.

    Not thread-safe — caller owns sequencing. Multiple sessions can exist
    at once, but the CLI player always runs exactly one at a time.
    """

    def __init__(self, audio_path: str | Path) -> None:
        self._path = str(audio_path)
        self._proc: subprocess.Popen | None = None
        self._paused = False

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def start(self) -> None:
        if not Path(AFPLAY_PATH).exists():
            raise RuntimeError(
                f"afplay not found at {AFPLAY_PATH}. v0 CLI playback is "
                "macOS-only; see docs/specs/2026-04-18-v0-cli-pivot-plan.md "
                "§Non-goals #2."
            )
        self._proc = subprocess.Popen([AFPLAY_PATH, self._path])
        self._paused = False

    def pause(self) -> None:
        if self._proc is None or self._paused:
            return
        self._proc.send_signal(signal.SIGSTOP)
        self._paused = True

    def resume(self) -> None:
        if self._proc is None or not self._paused:
            return
        self._proc.send_signal(signal.SIGCONT)
        self._paused = False

    def stop(self) -> None:
        if self._proc is None:
            return
        # If paused, SIGCONT first so SIGTERM can actually reap the process.
        if self._paused:
            self._proc.send_signal(signal.SIGCONT)
            self._paused = False
        self._proc.terminate()

    def wait(self, timeout: float | None = None) -> int:
        if self._proc is None:
            raise RuntimeError("wait() called before start()")
        return self._proc.wait(timeout=timeout)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_playback.py -v`
Expected: PASS (5/5).

- [ ] **Step 5: Commit**

```bash
git add player/__init__.py player/playback.py tests/test_playback.py
git commit -m "feat(player): afplay subprocess wrapper with SIGSTOP/SIGCONT pause"
```

### Task 2.2: `raw_key_reader` hotkey iterator

termios raw-mode context manager, yields single keypresses. Standalone, sync. Used from `asyncio.to_thread`.

**Files:**

- Create: `player/hotkeys.py`
- Create: `tests/test_hotkeys.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hotkeys.py`:

```python
"""Hotkey reader tests — fake stdin, verify termios raw mode is entered/exited.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Task 2.2
"""
from __future__ import annotations

from unittest import mock

from player.hotkeys import raw_key_reader, decode_key, KeyPress


def test_decode_single_chars():
    assert decode_key("l") == KeyPress.LIKE
    assert decode_key("s") == KeyPress.SKIP
    assert decode_key("r") == KeyPress.REPEAT
    assert decode_key("p") == KeyPress.PAUSE
    assert decode_key(" ") == KeyPress.PAUSE        # space also = pause
    assert decode_key("q") == KeyPress.QUIT
    assert decode_key("L") == KeyPress.LIKE         # case-insensitive
    assert decode_key("x") == KeyPress.UNKNOWN


def test_raw_key_reader_yields_and_restores_termios(monkeypatch):
    # Fake stdin: returns 'l', 's', 'q' then EOF ('').
    # Mock (not io.StringIO) — we need a working .fileno() too.
    fake_stdin = mock.Mock()
    fake_stdin.read.side_effect = ["l", "s", "q", ""]
    fake_stdin.fileno.return_value = 0
    monkeypatch.setattr("sys.stdin", fake_stdin)
    # Skip real termios calls in tests — verify via call recording.
    tcgetattr = mock.Mock(return_value=["saved"])
    tcsetattr = mock.Mock()
    setraw = mock.Mock()
    monkeypatch.setattr("player.hotkeys.termios.tcgetattr", tcgetattr)
    monkeypatch.setattr("player.hotkeys.termios.tcsetattr", tcsetattr)
    monkeypatch.setattr("player.hotkeys.tty.setraw", setraw)

    collected: list[KeyPress] = []
    with raw_key_reader() as keys:
        for key in keys:
            collected.append(key)
            if key == KeyPress.QUIT:
                break

    assert collected == [KeyPress.LIKE, KeyPress.SKIP, KeyPress.QUIT]
    tcgetattr.assert_called_once()
    setraw.assert_called_once()
    tcsetattr.assert_called_once_with(mock.ANY, mock.ANY, ["saved"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hotkeys.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'player.hotkeys'`.

- [ ] **Step 3: Create `player/hotkeys.py`**

```python
"""termios raw-mode single-key reader for the CLI player.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Task 2.2
"""
from __future__ import annotations

import enum
import sys
import termios
import tty
from collections.abc import Iterator
from contextlib import contextmanager


class KeyPress(str, enum.Enum):
    LIKE = "like"
    SKIP = "skip"
    REPEAT = "repeat"
    PAUSE = "pause"
    QUIT = "quit"
    UNKNOWN = "unknown"


_KEY_MAP: dict[str, KeyPress] = {
    "l": KeyPress.LIKE,
    "s": KeyPress.SKIP,
    "r": KeyPress.REPEAT,
    "p": KeyPress.PAUSE,
    " ": KeyPress.PAUSE,
    "q": KeyPress.QUIT,
}


def decode_key(char: str) -> KeyPress:
    """Pure function: char → KeyPress. Case-insensitive on letters."""
    return _KEY_MAP.get(char.lower(), KeyPress.UNKNOWN)


@contextmanager
def raw_key_reader() -> Iterator[Iterator[KeyPress]]:
    """Context manager that yields an iterator of KeyPress values from stdin.

    Enters termios raw mode on __enter__, restores on __exit__ (even on
    exception). Inner iterator reads one char at a time via sys.stdin.read(1)
    and decodes via _KEY_MAP.
    """
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        def _gen() -> Iterator[KeyPress]:
            while True:
                ch = sys.stdin.read(1)
                if not ch:
                    return
                yield decode_key(ch)
        yield _gen()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_hotkeys.py -v`
Expected: PASS (2/2).

- [ ] **Step 5: Commit**

```bash
git add player/hotkeys.py tests/test_hotkeys.py
git commit -m "feat(player): raw_key_reader termios hotkey iterator"
```

### Task 2.3: `play_episode` async orchestrator

The integration piece. Plays segments sequentially, races playback against hotkey input, dispatches actions, calls `on_feedback` for learning signals.

**Files:**

- Create: `player/cli_player.py`
- Create: `tests/test_cli_player.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_player.py`:

```python
"""CLI player integration tests — scripted hotkey sequences drive play_episode.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Task 2.3
"""
from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from player.cli_player import FeedbackSignal, play_episode
from player.hotkeys import KeyPress


@pytest.fixture
def fake_segments():
    """Three segments, minimal shape for the player."""
    return [
        {"segment_index": 0, "agent": "weather", "pitch_title": "Fog",    "url": "/tmp/0.mp3"},
        {"segment_index": 1, "agent": "calendar", "pitch_title": "Dentist", "url": "/tmp/1.mp3"},
        {"segment_index": 2, "agent": "youtube",  "pitch_title": "VTuber",  "url": "/tmp/2.mp3"},
    ]


def _scripted_keys(keys: list[KeyPress]):
    """Return an async function that yields the scripted keys."""
    async def _reader(queue: asyncio.Queue):
        for k in keys:
            await queue.put(k)
    return _reader


async def _fake_play_segment(segment, session_factory, key_queue, on_feedback,
                              user_id, episode_id):
    # Default: let the segment finish naturally (no hotkey).
    # Overridden per-test via monkeypatch.
    pass


def test_like_key_records_like_and_continues(fake_segments, monkeypatch):
    captured: list[FeedbackSignal] = []

    async def on_fb(sig: FeedbackSignal) -> None:
        captured.append(sig)

    # Stub AfplaySession to avoid spawning afplay.
    fake_session = mock.Mock()
    fake_session.start = mock.Mock()
    fake_session.wait = mock.Mock(return_value=0)
    fake_session.stop = mock.Mock()
    fake_session.pause = mock.Mock()
    fake_session.resume = mock.Mock()
    monkeypatch.setattr(
        "player.cli_player.AfplaySession",
        mock.Mock(return_value=fake_session),
    )

    # Scripted keypresses: 'l' on segment 0, then segments 1 and 2 finish naturally.
    async def fake_key_source():
        yield KeyPress.LIKE
        # No more keys — player proceeds naturally after each segment ends.

    monkeypatch.setattr("player.cli_player._run_key_reader", fake_key_source)

    async def main():
        await play_episode(
            fake_segments, user_id="dev", episode_id="ep-t",
            on_feedback=on_fb,
        )

    asyncio.run(main())

    # Segment 0 got a like, segments 1 and 2 passed through silently.
    assert len(captured) == 1
    assert captured[0].signal == "like"
    assert captured[0].agent == "weather"
    assert captured[0].segment_index == 0


def test_skip_key_terminates_current_and_advances(fake_segments, monkeypatch):
    captured: list[FeedbackSignal] = []

    async def on_fb(sig): captured.append(sig)

    fake_session = mock.Mock()
    monkeypatch.setattr(
        "player.cli_player.AfplaySession",
        mock.Mock(return_value=fake_session),
    )

    async def fake_key_source():
        yield KeyPress.SKIP   # on segment 0

    monkeypatch.setattr("player.cli_player._run_key_reader", fake_key_source)

    asyncio.run(play_episode(
        fake_segments, user_id="dev", episode_id="ep-t", on_feedback=on_fb,
    ))

    # Skip on segment 0 → stop, log 'skip' against weather, advance.
    assert any(sig.signal == "skip" and sig.agent == "weather" for sig in captured)
    fake_session.stop.assert_called()


def test_pause_is_not_a_learning_signal(fake_segments, monkeypatch):
    captured: list[FeedbackSignal] = []

    async def on_fb(sig): captured.append(sig)

    fake_session = mock.Mock()
    monkeypatch.setattr(
        "player.cli_player.AfplaySession",
        mock.Mock(return_value=fake_session),
    )

    async def fake_key_source():
        yield KeyPress.PAUSE
        yield KeyPress.PAUSE   # resume
        yield KeyPress.SKIP

    monkeypatch.setattr("player.cli_player._run_key_reader", fake_key_source)

    asyncio.run(play_episode(
        fake_segments, user_id="dev", episode_id="ep-t", on_feedback=on_fb,
    ))

    # Only the skip is a signal; the two pause toggles must not appear.
    assert all(sig.signal != "pause" for sig in captured)
    assert any(sig.signal == "skip" for sig in captured)
    fake_session.pause.assert_called()
    fake_session.resume.assert_called()


def test_quit_stops_and_returns_immediately(fake_segments, monkeypatch):
    captured: list[FeedbackSignal] = []

    async def on_fb(sig): captured.append(sig)

    fake_session = mock.Mock()
    monkeypatch.setattr(
        "player.cli_player.AfplaySession",
        mock.Mock(return_value=fake_session),
    )

    async def fake_key_source():
        yield KeyPress.QUIT

    monkeypatch.setattr("player.cli_player._run_key_reader", fake_key_source)

    asyncio.run(play_episode(
        fake_segments, user_id="dev", episode_id="ep-t", on_feedback=on_fb,
    ))

    # Quit on segment 0 → no signal, stop called, segments 1 and 2 never played.
    assert captured == []
    fake_session.stop.assert_called()
```

> **Reviewer note:** Driving an asyncio + subprocess integration from tests is inherently a bit ugly. If the test style above creates brittle coupling to `play_episode`'s internals (e.g., the `_run_key_reader` monkeypatch seam), feel free to restructure the tests around a public `KeySource` protocol injected via constructor / param — as long as the four invariants hold: (1) `l` → one `like` signal, no stop; (2) `s` → one `skip` signal + stop; (3) `p`/space → no signal; (4) `q` → no signal + stop + no further segments.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli_player.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'player.cli_player'`.

- [ ] **Step 3: Create `player/cli_player.py`**

```python
"""CLI player — plays segments sequentially, dispatches hotkeys, emits feedback.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Task 2.3
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from player.hotkeys import KeyPress, raw_key_reader
from player.playback import AfplaySession


@dataclass
class FeedbackSignal:
    user_id: str
    episode_id: str
    segment_index: int
    agent: str
    pitch_title: str
    signal: str  # "like" | "skip" | "replay"
    ts: str     # ISO 8601 UTC


# Maps a learning keypress to its signal name. Pause/quit/unknown are filtered
# upstream (they are NOT learning signals — locked 2026-04-18).
_LEARNING_SIGNAL: dict[KeyPress, str] = {
    KeyPress.LIKE: "like",
    KeyPress.SKIP: "skip",
    KeyPress.REPEAT: "replay",
}


async def _run_key_reader() -> AsyncIterator[KeyPress]:
    """Wrap the sync raw_key_reader in an async iterator via to_thread.

    Seam: tests monkeypatch this to a scripted async generator.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[KeyPress | None] = asyncio.Queue()

    def _pump() -> None:
        with raw_key_reader() as keys:
            for k in keys:
                loop.call_soon_threadsafe(queue.put_nowait, k)
                if k == KeyPress.QUIT:
                    return
        loop.call_soon_threadsafe(queue.put_nowait, None)

    task = asyncio.create_task(asyncio.to_thread(_pump))
    try:
        while True:
            k = await queue.get()
            if k is None:
                return
            yield k
            if k == KeyPress.QUIT:
                return
    finally:
        if not task.done():
            task.cancel()


async def play_episode(
    segments: list[dict],
    user_id: str,
    episode_id: str,
    on_feedback: Callable[[FeedbackSignal], Awaitable[None]],
) -> None:
    """Play each segment via afplay, listen for hotkeys, emit feedback.

    Semantics per hotkey reference table (spec §Phase 2).
    """
    key_source = _run_key_reader()
    key_task: asyncio.Task[KeyPress | None] | None = None
    quit_requested = False

    async def _next_key() -> KeyPress | None:
        try:
            return await key_source.__anext__()
        except (StopAsyncIteration, StopIteration):
            return None

    i = 0
    while i < len(segments) and not quit_requested:
        seg = segments[i]
        print(f"  ▶ [segment {seg['segment_index']}] {seg['agent']}: "
              f"{seg['pitch_title']}  (l=like  s=skip  r=repeat  p=pause  q=quit)")
        session = AfplaySession(seg["url"])
        session.start()
        playback_task = asyncio.create_task(asyncio.to_thread(session.wait))

        restart = False
        advance = False
        while not (advance or restart or quit_requested):
            if key_task is None:
                key_task = asyncio.create_task(_next_key())
            done, _ = await asyncio.wait(
                {playback_task, key_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if playback_task in done:
                # Segment finished naturally — advance.
                advance = True
                break
            if key_task in done:
                key = key_task.result()
                key_task = None
                if key is None or key == KeyPress.QUIT:
                    session.stop()
                    playback_task.cancel()
                    quit_requested = True
                    break
                if key == KeyPress.PAUSE:
                    if session.is_paused:
                        session.resume()
                    else:
                        session.pause()
                    continue
                if key == KeyPress.REPEAT:
                    await _emit(on_feedback, user_id, episode_id, seg, "replay")
                    session.stop()
                    playback_task.cancel()
                    restart = True
                    break
                if key == KeyPress.SKIP:
                    await _emit(on_feedback, user_id, episode_id, seg, "skip")
                    session.stop()
                    playback_task.cancel()
                    advance = True
                    break
                if key == KeyPress.LIKE:
                    await _emit(on_feedback, user_id, episode_id, seg, "like")
                    # Continue playing; do not break.
                    continue
                # UNKNOWN: ignore, keep playing.
                continue

        if restart:
            continue  # same i; replay the same segment.
        i += 1

    if key_task is not None and not key_task.done():
        key_task.cancel()


async def _emit(
    on_feedback: Callable[[FeedbackSignal], Awaitable[None]],
    user_id: str,
    episode_id: str,
    seg: dict,
    signal: str,
) -> None:
    await on_feedback(FeedbackSignal(
        user_id=user_id,
        episode_id=episode_id,
        segment_index=seg["segment_index"],
        agent=seg["agent"],
        pitch_title=seg["pitch_title"],
        signal=signal,
        ts=datetime.now(timezone.utc).isoformat(),
    ))
```

> **Implementer note:** The tests in Step 1 drive this via monkeypatching `_run_key_reader` to a scripted async generator. If the implementation in Step 3 diverges from the test shape, reconcile by keeping `_run_key_reader` as the public seam (tests patch `player.cli_player._run_key_reader`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli_player.py -v`
Expected: PASS (4/4). If the asyncio race in `play_episode` flakes on CI, bound `asyncio.wait` with a small timeout and retry — but prefer fixing the root cause, not masking it.

- [ ] **Step 5: Commit**

```bash
git add player/cli_player.py tests/test_cli_player.py
git commit -m "feat(player): play_episode async orchestrator with hotkey dispatch"
```

### Task 2.4: Feedback log writer + reader

Pure sync — append-only JSONL. Lives in `learning_loop/` because signals semantically belong there per `learning_loop/docs/DESIGN.md`.

**Files:**

- Create: `learning_loop/feedback_log.py`
- Create: `tests/test_feedback_log.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_feedback_log.py`:

```python
"""Feedback log (JSONL) tests — append + iter round-trip, malformed-line safety.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Task 2.4
"""
from __future__ import annotations

import json

from learning_loop.feedback_log import log_signal, iter_signals


def test_append_and_iter_round_trip(tmp_path, monkeypatch):
    log_path = tmp_path / "feedback.jsonl"
    monkeypatch.setattr("learning_loop.feedback_log._LOG_PATH", log_path)

    log_signal({
        "user_id": "dev", "episode_id": "ep-1", "segment_index": 0,
        "agent": "weather", "pitch_title": "Fog", "signal": "like",
        "ts": "2026-04-18T13:00:00+00:00",
    })
    log_signal({
        "user_id": "dev", "episode_id": "ep-1", "segment_index": 1,
        "agent": "calendar", "pitch_title": "Dentist", "signal": "skip",
        "ts": "2026-04-18T13:02:00+00:00",
    })

    records = list(iter_signals("dev"))
    assert len(records) == 2
    assert records[0]["agent"] == "weather"
    assert records[1]["signal"] == "skip"


def test_iter_filters_by_user_id(tmp_path, monkeypatch):
    log_path = tmp_path / "feedback.jsonl"
    monkeypatch.setattr("learning_loop.feedback_log._LOG_PATH", log_path)

    log_signal({"user_id": "dev", "episode_id": "e", "segment_index": 0,
                "agent": "weather", "pitch_title": "t", "signal": "like",
                "ts": "2026-04-18T13:00:00+00:00"})
    log_signal({"user_id": "other", "episode_id": "e", "segment_index": 0,
                "agent": "weather", "pitch_title": "t", "signal": "like",
                "ts": "2026-04-18T13:00:00+00:00"})

    dev_records = list(iter_signals("dev"))
    assert len(dev_records) == 1
    assert dev_records[0]["user_id"] == "dev"


def test_iter_skips_malformed_lines(tmp_path, monkeypatch):
    log_path = tmp_path / "feedback.jsonl"
    log_path.write_text(
        json.dumps({"user_id": "dev", "episode_id": "e", "segment_index": 0,
                    "agent": "weather", "pitch_title": "t", "signal": "like",
                    "ts": "2026-04-18T13:00:00+00:00"}) + "\n"
        + "this is not json\n"
        + json.dumps({"user_id": "dev", "episode_id": "e", "segment_index": 1,
                      "agent": "calendar", "pitch_title": "t", "signal": "skip",
                      "ts": "2026-04-18T13:01:00+00:00"}) + "\n"
    )
    monkeypatch.setattr("learning_loop.feedback_log._LOG_PATH", log_path)

    records = list(iter_signals("dev"))
    assert len(records) == 2
    assert [r["signal"] for r in records] == ["like", "skip"]


def test_iter_returns_empty_when_log_missing(tmp_path, monkeypatch):
    log_path = tmp_path / "nothing.jsonl"
    monkeypatch.setattr("learning_loop.feedback_log._LOG_PATH", log_path)
    assert list(iter_signals("dev")) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_feedback_log.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'learning_loop.feedback_log'`.

- [ ] **Step 3: Create `learning_loop/feedback_log.py`**

```python
"""Append-only JSONL feedback log for v0 CLI learning-signal capture.

Structure per record — matches the forthcoming EpisodeSignals shape from
learning_loop/docs/DESIGN.md as closely as is useful for v0:

    {
      "user_id": str,
      "episode_id": str,
      "segment_index": int,
      "agent": str,
      "pitch_title": str,
      "signal": "like" | "skip" | "replay",
      "ts": ISO 8601 UTC,
    }

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Task 2.4
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict

_LOG_PATH = Path.home() / ".config" / "radio-podcast" / "feedback.jsonl"


class FeedbackRecord(TypedDict):
    user_id: str
    episode_id: str
    segment_index: int
    agent: str
    pitch_title: str
    signal: str
    ts: str


def log_signal(record: FeedbackRecord) -> None:
    """Append one record to the JSONL log. Creates the file + parent dir."""
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False))
        f.write("\n")


def iter_signals(user_id: str) -> Iterator[FeedbackRecord]:
    """Yield records for one user_id. Malformed lines are silently skipped."""
    if not _LOG_PATH.exists():
        return
    with _LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("user_id") == user_id:
                yield rec
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_feedback_log.py -v`
Expected: PASS (4/4).

- [ ] **Step 5: Commit**

```bash
git add learning_loop/feedback_log.py tests/test_feedback_log.py
git commit -m "feat(learning-loop): JSONL feedback log writer + reader"
```

### Task 2.5: `hydrate_producer_memory` from log

Pure function over the log → `{agent: weight}` dict → `seed_producer_memory(user_id, weights)`.

**Files:**

- Create: `learning_loop/seed_from_feedback.py`
- Modify: `learning_loop/__init__.py`
- Create: `tests/test_seed_from_feedback.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_seed_from_feedback.py`:

```python
"""hydrate_producer_memory tests — weight computation + clamping.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Task 2.5
"""
from __future__ import annotations

import math
from unittest import mock

from learning_loop.seed_from_feedback import (
    compute_weights,
    hydrate_producer_memory,
)


def test_compute_weights_product_of_multipliers():
    records = [
        {"agent": "weather", "signal": "like"},    # ×1.10
        {"agent": "weather", "signal": "like"},    # ×1.10
        {"agent": "calendar", "signal": "skip"},   # ×0.90
        {"agent": "youtube", "signal": "replay"},  # ×1.20
    ]
    weights = compute_weights(records)
    assert math.isclose(weights["weather"], 1.10 * 1.10, rel_tol=1e-9)
    assert math.isclose(weights["calendar"], 0.90, rel_tol=1e-9)
    assert math.isclose(weights["youtube"], 1.20, rel_tol=1e-9)


def test_compute_weights_clamps_at_max():
    # Ten likes on weather → 1.10^10 ≈ 2.59 → clamped to 2.0.
    records = [{"agent": "weather", "signal": "like"} for _ in range(10)]
    weights = compute_weights(records)
    assert weights["weather"] == 2.0


def test_compute_weights_clamps_at_min():
    # Ten skips on calendar → 0.90^10 ≈ 0.349 → above 0.3, still above min.
    # Make it big enough to clamp: 0.90^15 ≈ 0.206 → clamped to 0.3.
    records = [{"agent": "calendar", "signal": "skip"} for _ in range(15)]
    weights = compute_weights(records)
    assert weights["calendar"] == 0.3


def test_compute_weights_ignores_unknown_signal():
    records = [
        {"agent": "weather", "signal": "like"},
        {"agent": "weather", "signal": "pause"},   # not a learning signal
        {"agent": "weather", "signal": "unknown"},
    ]
    weights = compute_weights(records)
    assert math.isclose(weights["weather"], 1.10, rel_tol=1e-9)


def test_compute_weights_empty_returns_empty_dict():
    assert compute_weights([]) == {}


def test_hydrate_calls_seed_producer_memory(monkeypatch):
    monkeypatch.setattr(
        "learning_loop.seed_from_feedback.iter_signals",
        lambda user_id: iter([
            {"agent": "weather", "signal": "like"},
            {"agent": "calendar", "signal": "skip"},
        ]),
    )
    seed = mock.Mock()
    monkeypatch.setattr(
        "learning_loop.seed_from_feedback.seed_producer_memory", seed
    )
    weights = hydrate_producer_memory("dev")
    seed.assert_called_once()
    args, _ = seed.call_args
    assert args[0] == "dev"
    assert "weather" in args[1] and "calendar" in args[1]
    assert weights == args[1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_seed_from_feedback.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'learning_loop.seed_from_feedback'`.

- [ ] **Step 3: Create `learning_loop/seed_from_feedback.py`**

```python
"""Hydrate ProducerMemory from the v0 feedback log.

Reads the JSONL log written by player.cli_player, computes a product of
SIGNAL_MULTIPLIERS per agent, clamps to [AGENT_WEIGHT_MIN, AGENT_WEIGHT_MAX],
and seeds ProducerMemory via the sanctioned demo seam.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Task 2.5
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from learning_loop import seed_producer_memory
from learning_loop.feedback_log import iter_signals
from producer.memory import (
    AGENT_WEIGHT_MAX,
    AGENT_WEIGHT_MIN,
    SIGNAL_MULTIPLIERS,
)


def compute_weights(records: Iterable[dict[str, Any]]) -> dict[str, float]:
    """Pure: records → per-agent weight = clamp(Π SIGNAL_MULTIPLIERS[signal])."""
    weights: dict[str, float] = {}
    for rec in records:
        agent = rec.get("agent")
        signal = rec.get("signal")
        if not isinstance(agent, str):
            continue
        mult = SIGNAL_MULTIPLIERS.get(signal)
        if mult is None:
            continue
        weights[agent] = weights.get(agent, 1.0) * mult
    for agent in weights:
        weights[agent] = max(AGENT_WEIGHT_MIN, min(AGENT_WEIGHT_MAX, weights[agent]))
    return weights


def hydrate_producer_memory(user_id: str) -> dict[str, float]:
    """Read feedback log for user, compute weights, seed ProducerMemory.

    Returns the weights so the caller can print a delta summary.
    """
    records = list(iter_signals(user_id))
    weights = compute_weights(records)
    seed_producer_memory(user_id, weights)
    return weights
```

- [ ] **Step 4: Export the public API from `learning_loop/__init__.py`**

Add (or confirm) these exports:

```python
# learning_loop/__init__.py
from learning_loop.feedback_log import log_signal, iter_signals  # noqa: F401
from learning_loop.seed_from_feedback import (  # noqa: F401
    compute_weights,
    hydrate_producer_memory,
)
# seed_producer_memory must already be defined in this module per the v0 stub
# contract — see learning_loop/docs/DESIGN.md. If it isn't yet, stub it as:
#
#   _SEEDED_WEIGHTS: dict[str, dict[str, float]] = {}
#   def seed_producer_memory(user_id: str, agent_weights: dict[str, float]) -> None:
#       _SEEDED_WEIGHTS[user_id] = dict(agent_weights)
#
# and wire the read path in producer.memory.load_producer_memory to consult
# _SEEDED_WEIGHTS when it exists. That's out of scope for this task ONLY if
# seed_producer_memory already exists; otherwise spec Task 2.5.5 (below).
```

- [ ] **Step 4.5: Verify `seed_producer_memory` exists in `learning_loop`**

Run: `grep -n "def seed_producer_memory" learning_loop/*.py`
Expected: one hit. If **none**, stop and stub it per the inline note above before continuing — and use the read path that `producer.memory.load_producer_memory` can consult. This sub-step may surface as a gap in the v0 stub; report back, don't silently stub without confirming.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_seed_from_feedback.py -v`
Expected: PASS (6/6).

- [ ] **Step 6: Commit**

```bash
git add learning_loop/seed_from_feedback.py learning_loop/__init__.py \
        tests/test_seed_from_feedback.py
git commit -m "feat(learning-loop): hydrate_producer_memory from v0 feedback log"
```

### Task 2.6: Wire player + hydration into `cli_main` + weight-delta summary

**Files:**

- Modify: `agents/orchestrator.py` (`cli_main` — hydrate call + player wiring + summary)

- [ ] **Step 1: Add hydration at `cli_main` startup**

Immediately after preflight (Task 1.3) and before the banner:

```python
from learning_loop.seed_from_feedback import hydrate_producer_memory
hydrated_weights = hydrate_producer_memory(args.user_id)
if hydrated_weights:
    print(f"[learning] hydrated ProducerMemory from feedback log — agent_weights:")
    for agent, w in sorted(hydrated_weights.items()):
        print(f"  {agent:<10} {w:.3f}")
```

This prints nothing on first run (empty log). On second run after the user hit hotkeys in run 1, it prints the seeded weights.

- [ ] **Step 2: Replace the audio-result print loop with `play_episode` call**

In the `elif os.environ.get("ELEVENLABS_API_KEY"):` branch of `cli_main` (around line 335-358 pre-pivot), after the `run_episode_pipeline` returns:

```python
# OLD (delete):
#   for seg_result in result.audio.segment_results:
#       print(f"  [segment {seg_result['segment_index']}] "
#             f"{seg_result['url']} ({seg_result['duration_ms']}ms)")

# NEW:
from learning_loop.feedback_log import log_signal
from player.cli_player import FeedbackSignal, play_episode
from dataclasses import asdict

# Build the segment view the player expects. Pull pitch_title from `selected`
# (the producer-selected pitches, same order as audio segments).
player_segments = [
    {
        "segment_index": seg_result["segment_index"],
        "agent": selected[seg_result["segment_index"]]["agent"],
        "pitch_title": selected[seg_result["segment_index"]]["title"],
        "url": seg_result["url"],
    }
    for seg_result in result.audio.segment_results
]

async def _on_feedback(sig: FeedbackSignal) -> None:
    log_signal(asdict(sig))

print("\n── Playback ── (l=like  s=skip  r=repeat  p=pause  q=quit) ──")
asyncio.run(play_episode(
    segments=player_segments,
    user_id=args.user_id,
    episode_id=episode_id,
    on_feedback=_on_feedback,
))
```

> **Reviewer note:** This nests `asyncio.run` inside `cli_main` (which already calls `asyncio.run(run_episode_pipeline(...))` a few lines above). That's fine — each `asyncio.run` is its own event loop, and they run sequentially. Do NOT try to share one loop; keep the pipeline and player in separate runs.

- [ ] **Step 3: Print end-of-episode weight-delta preview**

After `play_episode` returns (still inside `cli_main`):

```python
print("\n── Learning signals logged — next run will re-seed ProducerMemory ──")
# Compute the delta so the demonstrator can narrate it live.
from learning_loop.seed_from_feedback import compute_weights
from learning_loop.feedback_log import iter_signals
post_run_weights = compute_weights(list(iter_signals(args.user_id)))
if post_run_weights:
    print(f"[learning] next-run agent_weights:")
    for agent, w in sorted(post_run_weights.items()):
        prev = hydrated_weights.get(agent, 1.0)
        arrow = "↑" if w > prev else ("↓" if w < prev else "·")
        print(f"  {agent:<10} {prev:.3f} → {w:.3f}  {arrow}")
else:
    print("[learning] no learning signals captured this episode.")
```

- [ ] **Step 4: Extend `tests/test_orchestrator_cli.py` with a hydration-on-startup smoke test**

Append:

```python
def test_cli_main_hydrates_producer_memory_at_startup(monkeypatch):
    """Hydration runs after preflight but before the banner."""
    import agents.orchestrator as orch

    calls: list[str] = []
    monkeypatch.setattr(
        "learning_loop.seed_from_feedback.hydrate_producer_memory",
        lambda user_id: (calls.append(user_id), {})[1],
    )
    monkeypatch.setattr(
        "auth.preflight.ensure_agent_auth", lambda _n: None
    )
    monkeypatch.setattr(orch, "run_episode",
        lambda *a, **k: ({}, {"today_context": {}, "user_profile": None}))
    monkeypatch.setattr("producer.events.subscribe", lambda *_a, **_k: None)

    with pytest.raises(SystemExit):
        orch.cli_main(["--weather", "--no-llm", "--no-external",
                       "--user-id", "demo"])
    assert calls == ["demo"]
```

- [ ] **Step 5: Run all tests**

Run: `pytest tests/test_orchestrator_cli.py tests/test_playback.py tests/test_hotkeys.py tests/test_cli_player.py tests/test_feedback_log.py tests/test_seed_from_feedback.py tests/test_auth_preflight.py tests/test_agents_orchestrator.py -v`
Expected: PASS across all suites.

- [ ] **Step 6: Commit**

```bash
git add agents/orchestrator.py tests/test_orchestrator_cli.py
git commit -m "feat(orchestrator): wire CLI player + feedback log + weight-delta summary"
```

### Task 2.7: End-to-end learning-demo smoke test

**Files:**

- No code changes; manual verification that exercises the full demo arc.

- [ ] **Step 1: Reset the feedback log**

```bash
rm -f ~/.config/radio-podcast/feedback.jsonl
```

- [ ] **Step 2: Run episode A with all three agents**

```bash
python -m agents.orchestrator --weather --calendar --youtube --user-id demo
```

Expected:
- `[learning] hydrated ProducerMemory …` prints nothing the first time (no log).
- Pitches, producer selection, script generation, audio generation all run.
- `── Playback ──` banner appears with hotkey legend.
- Segment 0 begins playing via `afplay`.

Press `l` once on segment 0 (like), `s` on segment 1 (skip), `r` on segment 2 (repeat) then `l` on that repeat, `q` during segment 3 (quit).

Expected end-of-episode printout:
```
── Learning signals logged — next run will re-seed ProducerMemory ──
[learning] next-run agent_weights:
  weather     1.000 → 1.100  ↑
  calendar    1.000 → 0.900  ↓
  youtube     1.000 → 1.320  ↑
```
(exact agents depend on which segments each played)

- [ ] **Step 3: Inspect the feedback log**

Run: `cat ~/.config/radio-podcast/feedback.jsonl | wc -l`
Expected: 4 lines (one per learning-signal keypress; the `q` is not a signal).

- [ ] **Step 4: Run episode B with the same agents**

```bash
python -m agents.orchestrator --weather --calendar --youtube --user-id demo
```

Expected:
- Startup prints `[learning] hydrated ProducerMemory from feedback log — agent_weights: …` with the weights from episode A.
- `── Guaranteed slots ──` printout shows a different ordering vs. episode A (agents with positive weights bump priority; agents with negative weights get demoted).
- This is the "watch how episode 2 re-orders the lineup" demo beat.

- [ ] **Step 5: No commit expected** — verification only.

---

## Phase 3 — Docs sync

Cheap, high-signal. Do not skip — the webpage-selection assumption is sprinkled through README and TODOS.

### Task 3.1: Rewrite the `README.md` usage block

**Files:**

- Modify: `README.md`

- [ ] **Step 1: Read the current README usage section**

Run: `grep -n -E "usage|CLI|webpage|frontend" README.md`
Identify any prose that implies a webpage-based selection flow.

- [ ] **Step 2: Replace with a CLI-only usage block**

Example target block:

```markdown
## Usage (v0 — CLI only)

v0 is a CLI. Select internal agents with flags:

```bash
python -m agents.orchestrator --weather --calendar --youtube
```

Absent flag → that agent is skipped. You must pass at least one.

Auth prereqs — the CLI runs each the first time you activate its agent:

- `--weather`: browser geolocation flow (`python -m auth.weather`)
- `--calendar`: Google OAuth flow (`python auth/calendar_auth.py`)
- `--youtube`: v0 reads a dev probe dir; set `YOUTUBE_PROBE_DIR`
  or populate the default at `tmp/ydata/probe_1776208130`

Extra flags:

- `--no-llm`: skip Producer LLM (scripts-only, cheap smoke)
- `--no-external`: skip the external/Alices round + agentic payment

> Frontend selection UI is deferred to v1.
```

Preserve the rest of the README (architecture, memory refs, etc.).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): document v0 CLI-only usage and auth prereqs"
```

### Task 3.2: Move frontend work under a "v1" heading in `TODOS.md`

**Files:**

- Modify: `TODOS.md`

- [ ] **Step 1: Read the current TODOS.md**

Run: `sed -n '1,200p' TODOS.md`
Flag any bullet that mentions frontend, webpage, selection UI, or web player.

- [ ] **Step 2: Add an explicit v0/v1 section split**

Create (or refactor) two top-level headings:

```markdown
## v0 (CLI)

- (existing v0 items — producer, YouTube agent, pipeline, etc.)

## v1 (deferred)

- Webpage for agent selection (moves today's CLI flags into a UI)
- Web player for the produced audio
- Real-time like/skip/repeat via browser UI (replaces the v0 CLI hotkeys entirely)
- (any other frontend-adjacent items previously inline)
```

Move any frontend-adjacent bullets that were previously in v0 into v1. Do not delete — just relocate.

- [ ] **Step 3: Commit**

```bash
git add TODOS.md
git commit -m "docs(todos): split v0 CLI scope from deferred v1 frontend work"
```

### Task 3.3: Add a one-line v0 note to `CLAUDE.md`

**Files:**

- Modify: `CLAUDE.md`

- [ ] **Step 1: Append under `## Purpose` or create a `## Scope` heading**

```markdown
## Scope

- v0 is CLI-only (`python -m agents.orchestrator --<agent> …`).
  Webpage selection and frontend player are deferred to v1.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): note v0 CLI-only scope"
```

---

## Self-review checklist (run before handoff)

- [ ] Every task has exact file paths (no placeholders)
- [ ] Every test step shows the test body; every code step shows the code
- [ ] Agent-name ordering is consistent — `"weather"`, `"calendar"`, `"youtube"` — across `_INTERNAL_AGENT_ORDER`, `_CLASS_BY_NAME`, `_PREFLIGHT_BY_NAME`, README, TODOS
- [ ] `--no-external` semantics are preserved (see Non-goals)
- [ ] No changes below the CLI layer (`run_episode`, producer, pipeline, audio untouched — Phase 2 wires onto the existing `segment_results`, does not modify audio generation)
- [ ] Preflight runs *before* agent class import in `cli_main`
- [ ] YouTube preflight triggers live OAuth via `oauth_and_capture` (not dir-only check)
- [ ] Signal mapping — `l`→`like` (1.10×), `s`→`skip` (0.90×), `r`→`replay` (1.20×) — uses the exact keys in `producer/memory.py::SIGNAL_MULTIPLIERS` (mismatch = silent learning failure)
- [ ] Pause does NOT produce a feedback record — verified by `test_pause_is_not_a_learning_signal` in Task 2.3
- [ ] `compute_weights` clamps in `[AGENT_WEIGHT_MIN=0.3, AGENT_WEIGHT_MAX=2.0]` using the constants from `producer/memory.py:31-33` (no duplicated literals)
- [ ] End-to-end smoke test (Task 2.7) captures signals to disk and prints a weight-delta preview at end-of-run (the dev validation — per the 2026-04-18 (PM) addendum, the second-run re-ordering step is cut from the demo)
- [ ] Storage paths follow the addendum conventions: `./data/episodes/{id}/segment_{n}.mp3`, `./data/signals/{user}/{episode}.jsonl`, `./data/agent_memory/{user}/{agent}.json`, `./exports/episode-{id}.mp3`
- [ ] `episode_id = uuid4()` (not `ep-{int(time.time())}`)
- [ ] Auto-export MP3 on every run unless `--no-export`

---

## Decisions locked (from the 2026-04-18 spec review)

1. **Inline browser auto-open + printed URL fallback (Claude-Code-login UX).** Preflight calls `auth.weather.main()` / `auth.calendar_auth.main()` / `agents.youtube.capture.oauth_and_capture()` directly. Each launches the browser automatically *and* prints the authorization URL to stdout so the user can click it manually if the auto-open fails. No additional UX work: the fallback-URL print is already native to `google_auth_oauthlib.flow.InstalledAppFlow.run_local_server` (calendar + youtube) and to `auth/weather.py:147` ("Opening browser: {url}" before `webbrowser.open`).
2. **`--youtube` triggers live YouTube Data API OAuth.** For the demo, `--youtube` runs real OAuth via `agents/youtube/capture.py:oauth_and_capture` (scope: `youtube.readonly`) when the probe dir isn't yet populated. Matches the demo-auth design locked in 2026-04-16 at `agents/youtube/docs/DESIGN.md:570`.
3. **`--no-external` semantics preserved verbatim.** External/Alices round is default-on; `--no-external` skips it. External is Producer-invoked from the marketplace (`agents/orchestrator.py:231-280`), not pre-selected by the user, so no `--external`/`--alices` flag is added. Flipping this would churn the `payment.*`, `producer.external_decision.*`, and `producer.marketplace.*` wiring locked in the 2026-04-17 producer-alignment plan for zero demo-quality gain.
4. **v0 CLI player + learning-loop feedback (pulls v1 work forward).** Phase 2 adds `afplay`-based auto-playback plus termios-based hotkey capture (`l`/`s`/`r` signals; `p`/space pause — not a signal; `q` quit). Signals append to `feedback.jsonl`; next run hydrates `ProducerMemory` via the `seed_producer_memory` demo seam, producing visible episode-over-episode re-ordering. **macOS-only** (`/usr/bin/afplay`); cross-platform CLI is explicitly out of scope — v1 replaces the CLI outright with a frontend.
5. **Pause is not a learning signal.** `p` / space toggles `SIGSTOP`/`SIGCONT` on the afplay subprocess but writes nothing to the feedback log. Only `l` / `s` / `r` are learning signals.
6. **Execution mode: subagent-driven.** Fresh subagent per task with a two-stage review checkpoint between tasks (per `superpowers:subagent-driven-development`).

---

## Addendum 2026-04-18 (PM) — Local-filesystem storage (api-storage v1 deferral)

**Context.** With v0 now CLI-only (flag-driven agent activation, no frontend) and the learning-loop stubbed, the 10-min demo shows **one episode generation, not two**. This collapses api-storage's surface area.

**Decision.** The api-storage component as specified in `api-storage/docs/DESIGN.md` is **deferred to v1 except for a minimal local-filesystem slice**. No Next.js scaffold, no SSE stream, no Supabase project, no API routes get built for v0.

### Directory layout (final v0 on-disk conventions)

| Path                                                   | Purpose                             | Writer                                                       | Reader                                                   |
| ------------------------------------------------------ | ----------------------------------- | ------------------------------------------------------------ | -------------------------------------------------------- |
| `./data/episodes/{episode_id}/segment_{n}.mp3`         | Per-segment TTS output              | `audio/tts.py::TTSClient` (already defaults here — line 60)  | `player/cli_player.py`                                   |
| `./exports/episode-{episode_id}.mp3`                   | Concat'd judge-handoff MP3          | `storage/export.py::concat_episode_mp3` (end of CLI run)     | human (copy / Slack / email / Drive)                     |
| `./data/signals/{user_id}/{episode_id}.jsonl`          | Per-episode feedback signals        | `player.cli_player::on_feedback` → `storage.signals.append_signal` | `learning_loop.seed_from_feedback.hydrate_producer_memory` |
| `./data/agent_memory/{user_id}/{agent_name}.json`      | Per-agent memory (v0 scaffold only) | _unwritten in v0 (learning-loop stub)_                       | _helper exists (`storage.agent_memory.load_agent_memory`); not yet wired to agents_ |

**`episode_id` = `uuid.uuid4().__str__()`** generated once per CLI run in `cli_main`.

### What's cut from v0 (explicitly deferred to v1)

- Next.js scaffold and any HTTP server
- `/generate`, `/react`, `/episode/:id`, `/audio/:episode_id/:segment_n` routes
- Full SSE event schema (orchestration + memory-update events)
- Supabase Postgres schema (`agent_memory`, `episodes`, `signals` tables)
- `episodes` metadata persistence — CLI run state is ephemeral; only MP3 + memory JSON + signals JSONL survive
- HTTP Range streaming, demo-user validation, `/react` user guards, any auth
- **Second-run "memory update visible between runs" demo beat** — learning is shown in-run via hotkey capture + end-of-run weight-delta preview, not across two runs

### Rationale for JSONL-on-disk over Postgres in v0

No UI reads these files, no cross-episode learning-loop runs (stub), and the one-episode demo eliminates the "memory update visible between runs" drama. JSONL → Supabase is a one-day swap in v1 if the webpage returns.

---

### Phase 2S — Local-filesystem storage (4 tasks, land BEFORE Phase 2)

These tasks land before Phase 2's player work. Phase 2's Tasks 2.4 / 2.5 / 2.6 then import from `storage/*` (see §Overrides below).

#### Task 2S.1: `storage/episode_dir.py` + `.gitignore`

**Files:**

- Create: `storage/__init__.py`
- Create: `storage/episode_dir.py`
- Create: `tests/test_storage_episode_dir.py`
- Modify: `.gitignore`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_storage_episode_dir.py`:

```python
"""Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Addendum Task 2S.1"""
from __future__ import annotations

from pathlib import Path

from storage.episode_dir import episode_dir, new_episode_id


def test_new_episode_id_is_uuid_format():
    eid = new_episode_id()
    assert len(eid) == 36
    assert eid.count("-") == 4


def test_new_episode_id_is_unique():
    assert new_episode_id() != new_episode_id()


def test_episode_dir_creates_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.episode_dir._EPISODES_DIR", tmp_path / "episodes")
    d1 = episode_dir("abc-123")
    assert d1.exists() and d1.is_dir()
    d2 = episode_dir("abc-123")
    assert d2 == d1  # idempotent


def test_episode_dir_returns_path_under_data_episodes(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.episode_dir._EPISODES_DIR", tmp_path / "episodes")
    d = episode_dir("xyz")
    assert d.name == "xyz"
    assert d.parent.name == "episodes"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_storage_episode_dir.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'storage'`.

- [ ] **Step 3: Create `storage/__init__.py`**

```python
"""v0 local-filesystem storage — api-storage deferred to v1.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Addendum
"""
```

- [ ] **Step 4: Create `storage/episode_dir.py`**

```python
"""Episode directory conventions for v0 CLI storage.

Directory layout:
    ./data/episodes/{episode_id}/    — TTS segment output

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Addendum Task 2S.1
"""
from __future__ import annotations

import uuid
from pathlib import Path

_DATA_ROOT = Path("data")
_EPISODES_DIR = _DATA_ROOT / "episodes"


def new_episode_id() -> str:
    """Return a fresh uuid4 string. One call per CLI run."""
    return str(uuid.uuid4())


def episode_dir(episode_id: str) -> Path:
    """Return (and create on demand) ./data/episodes/{episode_id}/.

    Matches TTSClient's existing default output path layout
    (audio/tts.py:60 → `{output_dir}/{episode_id}/segment_{n}.mp3`).
    """
    d = _EPISODES_DIR / episode_id
    d.mkdir(parents=True, exist_ok=True)
    return d
```

- [ ] **Step 5: Add `.gitignore` entries**

Append to `.gitignore`:

```
# v0 CLI storage (local-filesystem; api-storage deferred to v1)
/data/
/exports/
```

If `.gitignore` doesn't exist yet, create it with the two lines above.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_storage_episode_dir.py -v`
Expected: PASS (4/4).

- [ ] **Step 7: Commit**

```bash
git add storage/__init__.py storage/episode_dir.py \
        tests/test_storage_episode_dir.py .gitignore
git commit -m "feat(storage): v0 episode_dir + uuid4 episode_id + .gitignore"
```

#### Task 2S.2: `storage/signals.py` — feedback log (replaces planned `learning_loop/feedback_log.py`)

**Files:**

- Create: `storage/signals.py`
- Create: `tests/test_storage_signals.py`

**Note:** This task supersedes what was planned as Task 2.4 (`learning_loop/feedback_log.py`). See §Overrides below — Task 2.4 is deleted; `hydrate_producer_memory` (Task 2.5) now imports from `storage.signals` instead.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_storage_signals.py`:

```python
"""Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Addendum Task 2S.2"""
from __future__ import annotations

import json

from storage.signals import append_signal, iter_signals


def _rec(user_id="dev", episode_id="ep-1", segment_index=0,
         agent="weather", pitch_title="Fog", signal="like",
         ts="2026-04-18T13:00:00+00:00"):
    return {
        "user_id": user_id, "episode_id": episode_id,
        "segment_index": segment_index, "agent": agent,
        "pitch_title": pitch_title, "signal": signal, "ts": ts,
    }


def test_append_and_iter_round_trip_single_episode(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.signals._SIGNALS_DIR", tmp_path / "signals")
    append_signal("dev", "ep-1", _rec(segment_index=0, agent="weather"))
    append_signal("dev", "ep-1", _rec(segment_index=1, agent="calendar", signal="skip"))

    recs = list(iter_signals("dev"))
    assert len(recs) == 2
    assert recs[0]["agent"] == "weather"
    assert recs[1]["signal"] == "skip"


def test_iter_signals_globs_across_episodes_for_user(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.signals._SIGNALS_DIR", tmp_path / "signals")
    append_signal("dev", "ep-1", _rec(episode_id="ep-1", agent="weather"))
    append_signal("dev", "ep-2", _rec(episode_id="ep-2", agent="youtube", signal="replay"))

    recs = list(iter_signals("dev"))
    # Ordered by filename sort (ep-1 before ep-2).
    assert [r["episode_id"] for r in recs] == ["ep-1", "ep-2"]


def test_iter_signals_isolates_users(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.signals._SIGNALS_DIR", tmp_path / "signals")
    append_signal("dev", "ep", _rec(user_id="dev"))
    append_signal("other", "ep", _rec(user_id="other"))

    assert len(list(iter_signals("dev"))) == 1
    assert list(iter_signals("dev"))[0]["user_id"] == "dev"


def test_iter_signals_skips_malformed_lines(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.signals._SIGNALS_DIR", tmp_path / "signals")
    user_dir = tmp_path / "signals" / "dev"
    user_dir.mkdir(parents=True)
    (user_dir / "ep-1.jsonl").write_text(
        json.dumps(_rec(signal="like")) + "\n"
        + "not json\n"
        + json.dumps(_rec(signal="skip")) + "\n"
    )
    recs = list(iter_signals("dev"))
    assert [r["signal"] for r in recs] == ["like", "skip"]


def test_iter_signals_empty_when_no_user_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.signals._SIGNALS_DIR", tmp_path / "signals")
    assert list(iter_signals("dev")) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_storage_signals.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'storage.signals'`.

- [ ] **Step 3: Create `storage/signals.py`**

```python
"""Per-episode feedback signal log (JSONL).

Path convention: ./data/signals/{user_id}/{episode_id}.jsonl
One file per episode; writer appends per-signal; reader globs all files
under the user dir (sorted by filename) for cross-episode hydration.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Addendum Task 2S.2
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict

_DATA_ROOT = Path("data")
_SIGNALS_DIR = _DATA_ROOT / "signals"


class FeedbackRecord(TypedDict):
    user_id: str
    episode_id: str
    segment_index: int
    agent: str
    pitch_title: str
    signal: str
    ts: str


def _signals_path(user_id: str, episode_id: str) -> Path:
    return _SIGNALS_DIR / user_id / f"{episode_id}.jsonl"


def append_signal(user_id: str, episode_id: str, record: FeedbackRecord) -> None:
    """Append one record to ./data/signals/{user_id}/{episode_id}.jsonl."""
    path = _signals_path(user_id, episode_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False))
        f.write("\n")


def iter_signals(user_id: str) -> Iterator[FeedbackRecord]:
    """Yield all records for a user across all episodes.

    Globs ./data/signals/{user_id}/*.jsonl (sorted by filename — lexicographic
    on uuid4 strings gives stable-but-arbitrary order; adequate for v0 since
    hydration is order-independent). Malformed lines are silently skipped.
    """
    user_dir = _SIGNALS_DIR / user_id
    if not user_dir.exists():
        return
    for path in sorted(user_dir.glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                yield rec
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_storage_signals.py -v`
Expected: PASS (5/5).

- [ ] **Step 5: Commit**

```bash
git add storage/signals.py tests/test_storage_signals.py
git commit -m "feat(storage): per-episode JSONL feedback log (replaces learning_loop/feedback_log.py)"
```

#### Task 2S.3: `storage/agent_memory.py` — per-agent memory scaffold

Helpers only. Not wired to agents in v0 (learning-loop stub doesn't write anywhere), but the convention is set in code so v1 migration is trivial.

**Files:**

- Create: `storage/agent_memory.py`
- Create: `tests/test_storage_agent_memory.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_storage_agent_memory.py`:

```python
"""Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Addendum Task 2S.3"""
from __future__ import annotations

from storage.agent_memory import load_agent_memory, save_agent_memory


def test_load_returns_empty_dict_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "storage.agent_memory._AGENT_MEM_DIR", tmp_path / "agent_memory"
    )
    assert load_agent_memory("dev", "weather") == {}


def test_save_then_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "storage.agent_memory._AGENT_MEM_DIR", tmp_path / "agent_memory"
    )
    memory = {"topic_multiplier": {"cooking": 1.3}, "schema_version": 1}
    save_agent_memory("dev", "weather", memory)
    assert load_agent_memory("dev", "weather") == memory


def test_load_returns_empty_on_malformed_json(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "storage.agent_memory._AGENT_MEM_DIR", tmp_path / "agent_memory"
    )
    # Write malformed content directly.
    path = tmp_path / "agent_memory" / "dev" / "weather.json"
    path.parent.mkdir(parents=True)
    path.write_text("not valid json {")
    assert load_agent_memory("dev", "weather") == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_storage_agent_memory.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'storage.agent_memory'`.

- [ ] **Step 3: Create `storage/agent_memory.py`**

```python
"""Per-agent memory scaffold (JSON). v0: read helper only; writes unused.

Path convention:
    ./data/agent_memory/{user_id}/{agent_name}.json

Per learning_loop/docs/DESIGN.md §v0 stub contract, nothing in v0 writes
agent memory — the stub contract guarantees empty/bootstrap reads. This
module exists so the v1 migration (unstubbing learning-loop) doesn't
need to introduce new path conventions: the file layout is already locked.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Addendum Task 2S.3
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DATA_ROOT = Path("data")
_AGENT_MEM_DIR = _DATA_ROOT / "agent_memory"


def _agent_memory_path(user_id: str, agent_name: str) -> Path:
    return _AGENT_MEM_DIR / user_id / f"{agent_name}.json"


def load_agent_memory(user_id: str, agent_name: str) -> dict[str, Any]:
    """Return parsed memory dict, or {} if the file is missing or malformed."""
    path = _agent_memory_path(user_id, agent_name)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_agent_memory(
    user_id: str, agent_name: str, memory: dict[str, Any]
) -> None:
    """Overwrite the agent's memory file. Unused in v0 (learning-loop stub)."""
    path = _agent_memory_path(user_id, agent_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(memory, indent=2, ensure_ascii=False))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_storage_agent_memory.py -v`
Expected: PASS (3/3).

- [ ] **Step 5: Commit**

```bash
git add storage/agent_memory.py tests/test_storage_agent_memory.py
git commit -m "feat(storage): per-agent memory scaffold (v0: read helper only)"
```

#### Task 2S.4: `storage/export.py` — ffmpeg concat for judge handoff

**Files:**

- Create: `storage/export.py`
- Create: `tests/test_storage_export.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_storage_export.py`:

```python
"""Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Addendum Task 2S.4"""
from __future__ import annotations

from unittest import mock

import pytest

from storage.export import concat_episode_mp3


def _setup_segments(tmp_path, monkeypatch, episode_id="ep-xyz", n=3):
    ep_dir = tmp_path / "episodes" / episode_id
    ep_dir.mkdir(parents=True)
    for i in range(n):
        (ep_dir / f"segment_{i}.mp3").write_bytes(b"fake mp3 bytes")
    monkeypatch.setattr("storage.episode_dir._EPISODES_DIR", tmp_path / "episodes")
    monkeypatch.setattr("storage.export._EXPORTS_DIR", tmp_path / "exports")
    return ep_dir


def test_concat_writes_to_exports_dir(tmp_path, monkeypatch):
    _setup_segments(tmp_path, monkeypatch)

    # Stub ffmpeg: pretend it succeeded and wrote the output.
    out_path_holder = {}

    def fake_run(args, capture_output, text):
        # Output path is last positional arg.
        out = args[-1]
        out_path_holder["out"] = out
        # Simulate ffmpeg actually creating the file.
        from pathlib import Path as _P
        _P(out).parent.mkdir(parents=True, exist_ok=True)
        _P(out).write_bytes(b"concat result")
        return mock.Mock(returncode=0, stderr="")

    monkeypatch.setattr("storage.export.shutil.which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr("storage.export.subprocess.run", fake_run)

    result = concat_episode_mp3("ep-xyz")
    assert result.exists()
    assert result.name == "episode-ep-xyz.mp3"
    assert result.parent.name == "exports"


def test_concat_raises_when_no_segments(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.episode_dir._EPISODES_DIR", tmp_path / "episodes")
    monkeypatch.setattr("storage.export._EXPORTS_DIR", tmp_path / "exports")
    (tmp_path / "episodes" / "ep-empty").mkdir(parents=True)

    with pytest.raises(RuntimeError, match="no segment_"):
        concat_episode_mp3("ep-empty")


def test_concat_raises_when_ffmpeg_missing(tmp_path, monkeypatch):
    _setup_segments(tmp_path, monkeypatch)
    monkeypatch.setattr("storage.export.shutil.which", lambda _: None)

    with pytest.raises(RuntimeError, match="ffmpeg not found"):
        concat_episode_mp3("ep-xyz")


def test_concat_raises_when_ffmpeg_returns_nonzero(tmp_path, monkeypatch):
    _setup_segments(tmp_path, monkeypatch)
    monkeypatch.setattr("storage.export.shutil.which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "storage.export.subprocess.run",
        lambda *a, **k: mock.Mock(returncode=1, stderr="ffmpeg: invalid syntax"),
    )
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        concat_episode_mp3("ep-xyz")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_storage_export.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'storage.export'`.

- [ ] **Step 3: Create `storage/export.py`**

```python
"""ffmpeg concat demuxer for judge-handoff MP3.

Input:  ./data/episodes/{episode_id}/segment_*.mp3 (sorted)
Output: ./exports/episode-{episode_id}.mp3

Uses ffmpeg's concat demuxer (needs a temporary filelist) so no re-encoding
happens — fast, lossless, preserves the ElevenLabs MP3s verbatim.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Addendum Task 2S.4
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from storage.episode_dir import episode_dir

_EXPORTS_DIR = Path("exports")


def concat_episode_mp3(episode_id: str) -> Path:
    """Concat all segment_*.mp3 in the episode dir into exports/episode-{id}.mp3.

    Raises RuntimeError if no segments exist, ffmpeg is missing, or ffmpeg
    returns non-zero.
    """
    src_dir = episode_dir(episode_id)
    segments = sorted(src_dir.glob("segment_*.mp3"))
    if not segments:
        raise RuntimeError(
            f"no segment_*.mp3 files found in {src_dir}; cannot export episode."
        )
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError(
            "ffmpeg not found on PATH. Install via `brew install ffmpeg` "
            "(macOS) and re-run; or skip export with `--no-export`."
        )

    _EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _EXPORTS_DIR / f"episode-{episode_id}.mp3"

    # Concat demuxer requires a newline-separated filelist with `file '…'` entries.
    # Use absolute paths to avoid cwd surprises.
    filelist = src_dir / "_concat_filelist.txt"
    filelist.write_text(
        "".join(f"file '{seg.resolve()}'\n" for seg in segments)
    )

    try:
        result = subprocess.run(
            [ffmpeg, "-y", "-f", "concat", "-safe", "0",
             "-i", str(filelist), "-c", "copy", str(out_path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg failed ({result.returncode}): {result.stderr[:500]}"
            )
    finally:
        filelist.unlink(missing_ok=True)

    return out_path
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_storage_export.py -v`
Expected: PASS (4/4).

- [ ] **Step 5: Commit**

```bash
git add storage/export.py tests/test_storage_export.py
git commit -m "feat(storage): ffmpeg concat demuxer for judge-handoff MP3"
```

---

### Overrides to existing tasks

These override specific content in the original (pre-addendum) Phase 0 and Phase 2 tasks. The controller (orchestrator of subagent dispatch) must bake these into the respective task prompts. Implementer subagents NEVER see the addendum as a separate thing to cross-reference — they only see the merged, final task.

| Task            | Action      | Override                                                                                                     |
| --------------- | ----------- | ------------------------------------------------------------------------------------------------------------ |
| **Task 0.2**    | modify      | Add `--no-export` flag to argparse alongside the agent flags. `store_true`; help: `"Skip ffmpeg concat step at end of run (dev iteration)."` |
| **Task 2.4**    | **DELETED** | `learning_loop/feedback_log.py` is not created — `storage/signals.py` (Task 2S.2) replaces it entirely. No test file `tests/test_feedback_log.py` either. The `learning_loop/__init__.py` re-exports block (Task 2.5 Step 4) drops the `feedback_log.*` re-exports. |
| **Task 2.5**    | modify      | (a) Import path change: `from storage.signals import iter_signals` (not `learning_loop.feedback_log`). (b) Test file change: `tests/test_seed_from_feedback.py` monkeypatches `storage.signals.iter_signals` — NOT `learning_loop.feedback_log.iter_signals`. (c) `learning_loop/__init__.py` re-exports `compute_weights` and `hydrate_producer_memory` only. |
| **Task 2.6**    | modify      | See full replacement block below.                                                                            |
| **Task 2.7**    | modify      | Delete original Steps 4 and 5 (second-run demo + commit). Keep Steps 1-3 as dev-time validation only — this is no longer a demo beat; see §Addendum Context. Step 2's expected output changes: feedback log lines are at `./data/signals/demo/{episode_id}.jsonl` (not `~/.config/radio-podcast/feedback.jsonl`). Step 3's `cat` path updates accordingly. Add a new Step 4: verify `./exports/episode-{episode_id}.mp3` was created by auto-export. |

#### Task 2.6 — FULL REPLACEMENT of Step 2 + Step 3

**Step 2 (replaces original "Replace the audio-result print loop with `play_episode` call"):**

In `cli_main`, replace the existing `episode_id = f"ep-{int(time.time())}"` line and the audio-result print loop with:

```python
# OLD (delete):
#   episode_id = f"ep-{int(time.time())}"
#   result = asyncio.run(run_episode_pipeline(selected, brief, episode_id))
#   …
#   for seg_result in result.audio.segment_results:
#       print(f"  [segment {seg_result['segment_index']}] "
#             f"{seg_result['url']} ({seg_result['duration_ms']}ms)")

# NEW:
from storage.episode_dir import new_episode_id
episode_id = new_episode_id()
print(f"[orchestrator] episode_id = {episode_id}")

result = asyncio.run(run_episode_pipeline(selected, brief, episode_id))
print(f"── Opener ──\n{result.opener}\n")

# Build the segment view the player expects.
player_segments = [
    {
        "segment_index": seg_result["segment_index"],
        "agent": selected[seg_result["segment_index"]]["agent"],
        "pitch_title": selected[seg_result["segment_index"]]["title"],
        "url": seg_result["url"],
    }
    for seg_result in result.audio.segment_results
]

# Feedback sink: append to per-episode JSONL under ./data/signals/{user}/.
from dataclasses import asdict
from player.cli_player import FeedbackSignal, play_episode
from storage.signals import append_signal

async def _on_feedback(sig: FeedbackSignal) -> None:
    append_signal(args.user_id, episode_id, asdict(sig))

print("\n── Playback ── (l=like  s=skip  r=repeat  p=pause  q=quit) ──")
asyncio.run(play_episode(
    segments=player_segments,
    user_id=args.user_id,
    episode_id=episode_id,
    on_feedback=_on_feedback,
))

# Auto-export concat MP3 unless --no-export.
if not args.no_export:
    from storage.export import concat_episode_mp3
    try:
        out = concat_episode_mp3(episode_id)
        print(f"\n[export] judge-handoff MP3 → {out}")
    except RuntimeError as e:
        print(f"\n[export] failed: {e}")
        print(f"[export] individual segments still available under "
              f"./data/episodes/{episode_id}/")
```

**Step 3 (replaces original "Print end-of-episode weight-delta preview"):**

Same body as the original Step 3 — no path changes — except the imports now read:

```python
from learning_loop.seed_from_feedback import compute_weights
from storage.signals import iter_signals   # ← was learning_loop.feedback_log
post_run_weights = compute_weights(list(iter_signals(args.user_id)))
```

Everything else in Step 3 (the arrow-rendering `↑/↓/·` loop, the "no learning signals" fallback) is unchanged.

---

### Updated Non-goals (append to the top §Non-goals list)

8. **api-storage component (Next.js + Supabase + SSE per `api-storage/docs/DESIGN.md`).** The full spec is deferred to v1. v0 ships the minimum filesystem slice defined in this addendum (§Phase 2S).
9. **Second-run "memory update visible between runs" demo beat.** Cut from the 10-min demo narrative. Learning is demonstrated by (a) live in-run signal capture visible in stdout and (b) end-of-run weight-delta preview ("these signals would yield weather: 1.00 → 1.21 on the next run"). The original Task 2.7 second-run step is retained only as dev-time validation.

### Updated Decisions locked (append)

7. **One-episode demo, filesystem-only persistence.** Signals = JSONL at `./data/signals/{user}/{episode}.jsonl`; agent memory = JSON at `./data/agent_memory/{user}/{agent}.json` (scaffold; v0 never writes); audio segments = MP3 at `./data/episodes/{id}/segment_{n}.mp3` (TTSClient already defaults here — audio/tts.py:60); judge handoff = `./exports/episode-{id}.mp3`. JSONL → Supabase is a one-day swap in v1.
8. **Auto-export MP3 on every run, `--no-export` for dev iteration.** Every CLI run ends with `concat_episode_mp3(episode_id)` → `./exports/episode-{id}.mp3`. Dev can skip via `--no-export` (added to Phase 0 Task 0.2).
9. **`episode_id` is `uuid.uuid4()` as a string, not a timestamp.** Replaces `f"ep-{int(time.time())}"` in `cli_main`. uuid4 avoids collision on same-second double-runs and matches the api-storage design's eventual Supabase PK convention.

# Agent: `calendar_agent`

**Status:** APPROVED — eng review cleared 2026-04-16
**Parent component doc:** [`../../docs/DESIGN.md`](../../docs/DESIGN.md) — `agents` component (shared `DataAgent` protocol, memory shape, `Pitch` shape)
**Session artifact:** `~/.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260416-205102.md`
**Scope:** demo (v0). Live Google Calendar API. Pitch carries raw rich event data for the Producer LLM — no taste logic in this agent.

## Purpose

Supply today's calendar events to the Brief (context pipe) and emit 1 schedule-context pitch to the Producer. The agent owns:

1. **Event acquisition** — fetch today's events from Google Calendar API via OAuth 2.0 (`calendar.readonly` scope).
2. **Context pipe** — populate `Brief.today_context.calendar_events` with flat event strings for other agents.
3. **Pitch generation** — pack `calendar_events_rich` into `Pitch.data`. No taste, no template logic, no LLM call. The Producer LLM receives the raw facts and decides how to voice them.

Calendar is a context agent, not a topic agent. It always emits exactly 1 pitch with `claim_kind="neutral"`, priority in the 0.5-0.65 range. It competes for segment time alongside YouTube, weather, and alices, but is never the star of the show.

## Architecture

```
fetch_context(user_id)
  |
  +-- Load OAuth token (~/.config/radio-podcast/calendar_token.json)
  |   Token expired -> auto-refresh
  |   Token revoked / missing -> api_reachable=False, return empty
  |
  +-- _list_events(credentials) -> Google Calendar API events.list(next 16h)
  |   RFC3339 datetimes normalized to 24h HH:MM throughout (per-event try/except)
  |   Max 20 events, cancelled events filtered, confirmed + tentative + all-day included
  |
  +-- ScopeContext output:
      {
        "api_reachable": bool,
        "calendar_events": ["Team standup 10:00am", ...],       # flat strings for Brief
        "calendar_events_rich": [                                # rich dicts for pitch()
          {
            "summary": "Team standup",
            "start": "10:00",
            "end": "10:30",
            "duration_min": 30,
            "attendee_count": 5,
            "is_recurring": true,
            "has_video_call": true,
            "organizer": "Alex Chen"
          },
          ...
        ]
      }

pitch(brief, memory, context, user_id)
  |
  +-- priority: 0.5 if api_reachable=False or 0 events
  |             0.55/0.60/0.65 for 1-3/4-6/7+ events
  +-- Pitch.data = {"api_reachable": bool, "events": calendar_events_rich}
  +-- Pitch.hook = "{n} calendar events today" | "Calendar unavailable"
      (Producer LLM reads data, not hook, to generate radio copy)
```

### Key design decisions

| Decision | Choice | Why |
|----------|--------|-----|
| No taste in pitch() | Raw rich data in `Pitch.data` | Producer LLM is the right place to decide how to voice calendar facts. Template hooks were pre-judging what matters. |
| `api_reachable` boolean | Distinguish API failure from empty calendar | Producer needs to know the difference: "API down" vs "genuinely empty day" call for different copy. |
| `_list_events()` wrapper | Thin function around Google API chain | Clean mock boundary for tests. One function to mock instead of `discovery.build().events().list().execute()`. |
| Per-event try/except | Skip malformed RFC3339 datetimes | One bad event shouldn't crash the entire agent. Log warning, keep going. |
| `calendar_events_rich` in ScopeContext | Agent-internal field alongside `calendar_events` | Orchestrator reads flat strings for Brief. pitch() forwards rich dicts to Producer via `Pitch.data`. |

### Data flow through the system

```
                  PHASE 1: fetch_context (parallel)
                  ================================
  Google Cal API                     calendar_agent
  +------------+    _list_events()   +----------------------------+
  | events.list|  ----------------> | normalize RFC3339 -> HH:MM |
  |  (today)   |                    | build flat + rich dicts     |
  +------------+                    | set api_reachable           |
                                    +----------------------------+
                                              |
                                    ScopeContext to orchestrator
                                              |
                  SYNC BARRIER: all fetch_context() done
                  ======================================
                                              |
                              orchestrator assembles Brief:
                              Brief.today_context.calendar_events
                              = context["calendar_events"]
                                              |
                  PHASE 2: pitch (parallel, all agents get same Brief)
                  ===================================================
                                              |
                              calendar_agent.pitch()
                              reads context["calendar_events_rich"]
                              reads context["api_reachable"]
                              emits 1 Pitch (template hook)
                                              |
                              select_segments() -> running order
```

## Google Calendar API Integration

**Auth:** `google-api-python-client` with OAuth 2.0, `calendar.readonly` scope.

**Token lifecycle:**
- **First run (in-app):** When user selects the Calendar agent on the agent selection screen and no token exists, the app triggers the OAuth consent flow inline. The browser opens Google's consent page; user approves `calendar.readonly`; token is stored at `~/.config/radio-podcast/calendar_token.json` (gitignored). This happens as step 2 of the sequential auth flow (after YouTube OAuth, before weather GPS). See `agents/docs/DESIGN.md` §Agent Selection & Auth Sequence.
- **Fallback setup:** `auth/calendar.py` can still run the consent flow standalone for dev/testing.
- **Runtime:** `fetch_context()` loads token, auto-refreshes if expired via `google.auth.transport.requests.Request()`.
- **Revoked:** If refresh fails, log warning, set `api_reachable=False`, return empty events. Next episode generation re-triggers consent flow.
- **Demo moment:** The OAuth consent popup is visible on screen. Judge sees the demonstrator approve Google Calendar access, then hears real calendar data in the podcast ~60 seconds later.

**Event filtering:**
- Time range: next 16 hours from current time (UTC rolling window — fetches upcoming events, not past)
- Max events: 20
- Filter out: cancelled events
- Include: confirmed + tentative
- All-day events: included. Flat string: `"{summary} (all day)"`. Rich dict: `start="all-day"`, `end="all-day"`, `duration_min=0`.

## Priority mapping

Priority is scheduling metadata, not taste. It scales with event count so the Producer's segment selector can weigh calendar context against other pitches.

| Event count | Priority | Rationale |
|-------------|----------|-----------|
| 0 / unreachable | 0.50 | Nothing actionable |
| 1-3         | 0.55     | Light day |
| 4-6         | 0.60     | Moderate day |
| 7+          | 0.65     | Busy day |

0.7 cap: calendar never competes above mid-range.

## Fallback Behavior

| Condition | api_reachable | Pitch.data["events"] | Priority | LLM call? |
|-----------|---------------|----------------------|----------|-----------|
| API failure (auth, network, quota) | false | [] | 0.5 | No |
| API success, 0 events | true | [] | 0.5 | No |
| API success, N events | true | N rich dicts | 0.55-0.65 | No |

## Orchestrator changes

`run_episode()` returns `(pitches_by_agent, brief)` tuple instead of just `pitches_by_agent`. This lets the CLI (and any future caller) use the real Brief for the Producer LLM pass instead of reconstructing it with hardcoded data.

**Before:** `orchestrator.py:172-186` hardcodes `"partly cloudy, 18C"` and fake calendar events.
**After:** CLI receives the Brief that `run_episode()` already builds internally from Phase 1 context.

## Protocol compliance

- Implements `DataAgent` protocol (agents/protocol.py)
- `load_memory()`: returns `bootstrap_memory()` (no calendar-specific memory in v0)
- `fetch_context(user_id)`: returns `ScopeContext` with `api_reachable`, `calendar_events`, `calendar_events_rich`
- `pitch(brief, memory, context, user_id)`: returns `list[Pitch]` with exactly 1 pitch; `Pitch.data = {"api_reachable": bool, "events": list[dict]}`
- `claim_kind`: always `"neutral"`
- `provenance_shape`: always `"balanced"` (no-op for non-taste agents, kept for protocol compliance)
- `thin_signal`: always `False` (calendar always has data, even if that data is "empty calendar")

## Time format convention

24h `"HH:MM"` everywhere — rich dict `start`/`end`, flat strings in `calendar_events`, and hook text. Uniform format enables gap arithmetic in `_build_hook` without parsing am/pm suffixes. The Producer LLM rewrites all time references into spoken radio voice anyway.

## Open questions

1. **Tomorrow preview:** Fetch tomorrow's first event for "heads up, early start" hooks. Deferred to v1.
2. **Attendee privacy:** Names go into template hooks. Fine for demo, needs privacy policy for production.

## Deferred to v1

- **Calendar LLM:** Add an LLM pass in `pitch()` if template hooks aren't editorial enough. Follow YouTube's `llm.py` pattern.
- **Tempo signal:** Calendar emits `{pace: "compressed"|"relaxed", max_segment_sec: int}` that Producer uses to adjust episode-wide pacing. See TODOS.md.
- **Pattern detector:** Pre-LLM layer that detects back-to-backs, gaps, density. Only needed if LLM is added and raw events aren't structured enough.
- **Persistent memory:** Calendar-specific memory (e.g., "user always has standup at 10am, don't keep mentioning it").

## Dependencies

| Dependency | Version | Purpose |
|------------|---------|---------|
| `google-api-python-client` | latest | Google Calendar API client |
| `google-auth-httplib2` | latest | Auth transport |
| `google-auth-oauthlib` | latest | OAuth consent flow |

Install: `pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib`

## Test plan

See `~/.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-eng-review-test-plan-20260416-211516.md` for the full test plan.

**Test file:** `tests/test_calendar_agent.py`

Key test cases:
- `fetch_context()`: mock `_list_events()`, verify flat + rich output shapes, api_reachable paths, RFC3339 normalization, event filtering, max 20 cap
- `pitch()`: api_reachable=False, 0 events, N events; verify `Pitch.data` shape and priority by event count
- `select_segments()`: add case for calendar pitch competing in running order

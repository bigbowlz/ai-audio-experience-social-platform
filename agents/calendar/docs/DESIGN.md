# Agent: `calendar_agent`

**Status:** APPROVED — eng review cleared 2026-04-16
**Parent component doc:** [`../../docs/DESIGN.md`](../../docs/DESIGN.md) — `agents` component (shared `DataAgent` protocol, memory shape, `Pitch` shape)
**Session artifact:** `~/.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260416-205102.md`
**Scope:** demo (v0). Live Google Calendar API + deterministic template pitch.

## Purpose

Supply today's calendar events to the Brief (context pipe) and emit 1 schedule-context pitch to the Producer. The agent owns:

1. **Event acquisition** — fetch today's events from Google Calendar API via OAuth 2.0 (`calendar.readonly` scope).
2. **Context pipe** — populate `Brief.today_context.calendar_events` with flat event strings for other agents.
3. **Pitch generation** — deterministic template hooks using rich event data (attendee count, duration, recurrence, video call presence). No LLM call.

Calendar is a context agent, not a topic agent. It always emits exactly 1 pitch with `claim_kind="neutral"`, priority in the 0.5-0.65 range. It competes for segment time alongside YouTube, weather, and alices, but is never the star of the show.

## Architecture

```
fetch_context(user_id)
  |
  +-- Load OAuth token (~/.config/radio-podcast/calendar_token.json)
  |   Token expired -> auto-refresh
  |   Token revoked / missing -> api_reachable=False, return empty
  |
  +-- _list_events(credentials) -> Google Calendar API events.list(today)
  |   RFC3339 datetimes normalized to local HH:MM (per-event try/except)
  |   Max 20 events, cancelled events filtered, confirmed + tentative included
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
  +-- api_reachable=False -> "Couldn't reach your calendar today." (priority 0.5)
  +-- No events            -> "Your calendar is clear today."      (priority 0.5)
  +-- Events exist         -> Template hook from rich event data   (priority 0.55-0.65)
```

### Key design decisions

| Decision | Choice | Why |
|----------|--------|-----|
| No LLM in pitch() | Template hooks only | Producer LLM rewrites hooks into radio script. Two LLM calls for one calendar sentence is wasteful. Add LLM later if templates hurt the demo. |
| `api_reachable` boolean | Distinguish API failure from empty calendar | "Couldn't reach your calendar" vs "calendar is clear" is honest output for a technical audience. |
| `_list_events()` wrapper | Thin function around Google API chain | Clean mock boundary for tests. One function to mock instead of `discovery.build().events().list().execute()`. |
| Per-event try/except | Skip malformed RFC3339 datetimes | One bad event shouldn't crash the entire agent. Log warning, keep going. |
| `calendar_events_rich` in ScopeContext | Agent-internal field alongside `calendar_events` | Orchestrator reads flat strings for Brief. pitch() reads rich dicts for templates. Two fields because the shapes differ, not because the data differs. |

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
- **Fallback setup:** `scripts/calendar_auth.py` can still run the consent flow standalone for dev/testing.
- **Runtime:** `fetch_context()` loads token, auto-refreshes if expired via `google.auth.transport.requests.Request()`.
- **Revoked:** If refresh fails, log warning, set `api_reachable=False`, return empty events. Next episode generation re-triggers consent flow.
- **Demo moment:** The OAuth consent popup is visible on screen. Judge sees the demonstrator approve Google Calendar access, then hears real calendar data in the podcast ~60 seconds later.

**Event filtering:**
- Time range: today 00:00 to 23:59 in user's timezone
- Max events: 20
- Filter out: cancelled events
- Include: confirmed + tentative
- All-day events: TBD (open question, may include as "Today is also [event name]")

## Template Hook Design

Template hooks are deterministic (no LLM). They use `calendar_events_rich` fields to generate hooks richer than the current stub.

**Constraints:**
- Only reference events that exist in the input. Do not fabricate.
- `claim_kind="neutral"` always. No temporal claims ("you've been busy lately").
- Conversational voice. "Back-to-back morning with 3 meetings, then wide open after 2pm" not "You have 3 meetings in the morning."

**Priority mapping:**

| Event count | Priority | Rationale |
|-------------|----------|-----------|
| 0           | 0.50     | Open-day filler, likely deprioritized |
| 1-3         | 0.55     | Light day, mild relevance |
| 4-6         | 0.60     | Moderate day |
| 7+          | 0.65     | Busy day, worth mentioning |

0.7 cap is a guard for future priority logic changes. Calendar never competes above mid-range.

## Fallback Behavior

| Condition | api_reachable | Pitch text | Priority | LLM call? |
|-----------|---------------|------------|----------|-----------|
| API failure (auth, network, quota) | false | "Couldn't reach your calendar today." | 0.5 | No |
| API success, 0 events | true | "Your calendar is clear today..." | 0.5 | No |
| API success, N events | true | Template hook from rich data | 0.55-0.65 | No |

## Orchestrator changes

`run_episode()` returns `(pitches_by_agent, brief)` tuple instead of just `pitches_by_agent`. This lets the CLI (and any future caller) use the real Brief for the Producer LLM pass instead of reconstructing it with hardcoded data.

**Before:** `orchestrator.py:172-186` hardcodes `"partly cloudy, 18C"` and fake calendar events.
**After:** CLI receives the Brief that `run_episode()` already builds internally from Phase 1 context.

## Protocol compliance

- Implements `DataAgent` protocol (agents/protocol.py)
- `load_memory()`: returns `bootstrap_memory()` (no calendar-specific memory in v0)
- `fetch_context(user_id)`: returns `ScopeContext` with `api_reachable`, `calendar_events`, `calendar_events_rich`
- `pitch(brief, memory, context, user_id)`: returns `list[Pitch]` with exactly 1 pitch
- `claim_kind`: always `"neutral"`
- `provenance_shape`: always `"balanced"` (no-op for non-taste agents, kept for protocol compliance)
- `thin_signal`: always `False` (calendar always has data, even if that data is "empty calendar")

## Open questions

1. **All-day events:** Include (as "Today is also [event name]") or filter? TBD during implementation.
2. **Tomorrow preview:** Fetch tomorrow's first event for "heads up, early start" hooks. Deferred to v1.
3. **Attendee privacy:** Names go into template hooks. Fine for demo, needs privacy policy for production.

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
- `pitch()`: all three branches (api_reachable=False, no events, events exist), priority parametrized by event count
- `select_segments()`: add case for calendar pitch competing in running order

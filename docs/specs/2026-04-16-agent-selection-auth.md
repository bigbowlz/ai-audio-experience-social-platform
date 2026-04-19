# Agent Selection & Inline Auth Flow

**Status:** APPROVED — brainstorming cleared 2026-04-16
**Parent docs:**

- Master: `~/.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md` (P2, P7, P10)
- Agents: `agents/docs/DESIGN.md` §Agent Selection & Auth Sequence
- API: `api-storage/docs/DESIGN.md` §API routes
  **Scope:** demo (v0). Agent selection landing page + sequential OAuth/GPS auth flow before episode generation.

## Purpose

The agent selection screen is the app's landing page. The user picks which agents to enable, then the app runs sequential auth flows for each selected agent before firing `POST /generate`. Each auth step is visible to the demo audience — this IS the "real agents with real data" demo moment (P7, P9).

## Architecture

```
Landing Page (agent selection)
  |
  +-- 4 agent cards: YouTube, Calendar, Weather, Alice
  |   Each card: icon, name, scope description, auth status badge
  |   User toggles cards on/off
  |
  +-- "Generate Episode" button
  |   |
  |   +-- onClick -> sequential auth pipeline:
  |       |
  |       1. YouTube selected + no token?
  |       |   -> redirect to /api/auth/youtube (server-side OAuth)
  |       |   -> Google consent page -> callback stores token -> return to app
  |       |
  |       2. Calendar selected + no token?
  |       |   -> redirect to /api/auth/calendar (server-side OAuth)
  |       |   -> Google consent page -> callback stores token -> return to app
  |       |
  |       3. Weather selected + no location?
  |       |   -> browser navigator.geolocation.getCurrentPosition()
  |       |   -> store lat/lon in memory
  |       |
  |       4. Alice -> no setup needed
  |       |
  |       +-- All auth done -> POST /generate { selected_agents, ... }
  |           -> SSE stream begins -> transition to generation/player view
```

## Auth API Routes

New Next.js API routes in `api-storage/`:

```typescript
// GET /api/auth/status
// -> { youtube: bool, calendar: bool, weather: { lat: number, lon: number } | null }
// Frontend calls on page load to populate auth badges on cards.
// Checks token file existence on disk for YouTube/Calendar.

// GET /api/auth/youtube
// -> 302 redirect to Google OAuth consent (youtube.readonly scope)
// Sets state param with return URL for CSRF protection.

// GET /api/auth/youtube/callback?code=...&state=...
// -> exchanges authorization code for tokens via Google token endpoint
// -> stores token at ~/.config/radio-podcast/youtube_token.json
// -> 302 redirect back to app with ?auth=youtube_ok query param

// GET /api/auth/calendar
// -> 302 redirect to Google OAuth consent (calendar.readonly scope)

// GET /api/auth/calendar/callback?code=...&state=...
// -> exchanges code for tokens
// -> stores at ~/.config/radio-podcast/calendar_token.json
// -> 302 redirect back to app with ?auth=calendar_ok
```

### Key decisions

| Decision                         | Choice                                                  | Why                                                                                                                           |
| -------------------------------- | ------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| Server-side redirect (not popup) | Standard OAuth redirect flow                            | Avoids popup blockers. Google recommends redirect for web apps.                                                               |
| Token stored on disk             | `~/.config/radio-podcast/{youtube,calendar}_token.json` | Python agents already read from these paths. No DB for auth. Localhost single-user.                                           |
| Single Google Cloud OAuth client | One client ID, two separate consent redirects           | Different scopes (`youtube.readonly` vs `calendar.readonly`). Sequential redirects = judge sees each permission individually. |
| Weather GPS is browser-native    | `navigator.geolocation.getCurrentPosition()`            | No server round-trip. Stored in app state (in-memory for demo).                                                               |
| Alice always "Ready"           | Pre-captured Day-0 data in repo                         | No auth step. Card shows green badge.                                                                                         |

## Frontend Auth Flow State Machine

```
IDLE
  -> (user clicks Generate)
  -> CHECK_STATUS        GET /api/auth/status
  -> YOUTUBE_AUTH        if selected + no token -> redirect to Google
  -> YOUTUBE_DONE        callback returns ?auth=youtube_ok
  -> CALENDAR_AUTH       if selected + no token -> redirect to Google
  -> CALENDAR_DONE       callback returns ?auth=calendar_ok
  -> WEATHER_GPS         if selected + no location -> geolocation prompt
  -> WEATHER_DONE        lat/lon stored in app state
  -> GENERATING          POST /generate { selected_agents, target_length_sec }
                         -> SSE stream -> transition to player view
```

Steps for unselected agents or agents with existing tokens are skipped.

Each step shows a progress indicator on the agent card:

- "Connecting YouTube..." -> checkmark
- "Connecting Calendar..." -> checkmark
- "Getting location..." -> checkmark
- "Generating episode..."

## Agent Cards

Each card displays:

| Field      | YouTube                             | Calendar                         | Weather                        | Alice                   |
| ---------- | ----------------------------------- | -------------------------------- | ------------------------------ | ------------------------- |
| Icon       | YouTube logo                        | Calendar icon                    | Weather icon                   | Alice avatar            |
| Name       | @YouTube                            | @Calendar                        | @Weather                       | @GoddamnAxl               |
| Scope      | "Your subscriptions & liked videos" | "Today's Google Calendar events" | "Local weather conditions"     | "Alice's curated picks" |
| Auth badge | "Connected" / "Not connected"       | "Connected" / "Not connected"    | "Location set" / "No location" | "Ready" (always)          |
| Toggle     | on/off                              | on/off                           | on/off                         | on/off                    |

Cards default to all selected (demo flow: demonstrator picks all four).

## Error Handling

| Failure                                      | Behavior                                                               |
| -------------------------------------------- | ---------------------------------------------------------------------- |
| User denies YouTube OAuth                    | Card shows "Skipped", removed from `selected_agents`, continue to next |
| User denies Calendar OAuth                   | Card shows "Skipped", removed from `selected_agents`, continue to next |
| User denies GPS                              | Card shows "Skipped", removed from `selected_agents`, continue to next |
| OAuth callback error (network, Google error) | Card shows "Failed -- tap to retry", user can retry that agent         |
| All agents skipped/denied                    | "Generate" button disabled, message: "Select at least one agent"       |
| Token exists but expired                     | `/api/auth/status` returns false, auth flow re-triggers on Generate    |

Graceful degradation: denied agents are excluded, episode generates with whatever remains.

## Interaction with Existing Components

- **Python agents** read tokens from the same disk paths the auth callbacks write to. No new integration needed — `_load_credentials()` in each agent already handles these files.
- **`POST /generate`** already accepts `selected_agents: string[]` (see `api-storage/docs/DESIGN.md`). The auth flow filters the list before calling generate.
- **SSE stream** begins after auth completes. `episode.started` event includes `selected_agents` reflecting only the agents that passed auth.
- **`auth/calendar.py`** and any future `auth/youtube.py` remain available as dev/testing fallbacks. The in-app flow supersedes them for demo use.

## Open Questions

1. **Combined Google consent:** Could request both `youtube.readonly` + `calendar.readonly` in a single consent popup. Faster, but less dramatic for demo. Current decision: separate (sequential is better narrative). Revisit if auth flow feels too long.
2. **Persist weather location:** Currently in-memory only. If the app reloads, GPS re-prompts. For demo this is fine (single session). v1: persist to user profile in Supabase.
3. **Auth state across page refresh:** Query params (`?auth=youtube_ok`) are consumed once and cleared. If the user refreshes mid-auth-flow, `/api/auth/status` re-checks token files and resumes from where they left off.

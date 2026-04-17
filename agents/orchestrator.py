"""Episode orchestrator: sync barrier + Brief assembly + pitch dispatch.

Flow (per agents/docs/prompt_design.md §3):

  Phase 1 — fetch_context (parallel)
      weather_agent.fetch() ──► ScopeContext ──┐
      calendar_agent.fetch() ──► ScopeContext ──┤──► orchestrator assembles Brief
      youtube_agent.fetch() ──► ScopeContext    │
      (alices_agent stub) ──► ScopeContext    │
            ║                                   │
            ║  SYNC BARRIER: wait for ALL       │
            ╚═══════════════════════════════════╝

  Phase 2 — pitch (parallel, all receive same Brief)
      each agent.pitch(brief, memory, context, user_id) ──► list[Pitch]

Usage (CLI):
    python -m agents.orchestrator
"""

from __future__ import annotations

import concurrent.futures
import json
import os
from datetime import datetime
from typing import TYPE_CHECKING

from agents.protocol import AgentMemory, Brief, DataAgent, Pitch, ScopeContext, TodayContext

if TYPE_CHECKING:
    pass


# ── Time-of-day helper ────────────────────────────────────────────────

def _time_of_day(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


# ── Orchestrator ──────────────────────────────────────────────────────

def run_episode(
    agents: list[DataAgent],
    user_id: str,
) -> tuple[dict[str, list[Pitch]], Brief]:
    """Run one episode generation pass.

    Returns (pitches_by_agent, brief) — the Brief assembled from Phase 1
    is returned so callers can pass it to select_segments() and then to
    the Producer's LLM pass without reconstructing it.
    """
    # ── Phase 1: parallel fetch_context + load_memory ────────────────
    # Both calls are parallel per agent; we wait for ALL before moving on.
    with concurrent.futures.ThreadPoolExecutor() as pool:
        ctx_futures = {a.name: pool.submit(a.fetch_context, user_id) for a in agents}
        mem_futures = {a.name: pool.submit(a.load_memory, user_id) for a in agents}

        # .result() blocks; exceptions propagate here so the caller sees them.
        contexts: dict[str, ScopeContext] = {
            name: f.result() for name, f in ctx_futures.items()
        }
        memories: dict[str, AgentMemory] = {
            name: f.result() for name, f in mem_futures.items()
        }

    # ── SYNC BARRIER: all fetch_context() done ────────────────────────
    # Assemble Brief.today_context from weather + calendar ScopeContext fields.
    # Use local time so date/day/time_of_day reflect the user's wall clock.
    now = datetime.now()

    weather_summary: str | None = None
    calendar_events: list[str] | None = None

    for a in agents:
        ctx = contexts[a.name]
        if a.name == "weather":
            weather_summary = ctx.get("weather_summary")  # type: ignore[call-overload]
        elif a.name == "calendar":
            calendar_events = ctx.get("calendar_events")  # type: ignore[call-overload]

    today_context: TodayContext = {
        "date": now.date().isoformat(),
        "day_of_week": now.strftime("%A"),
        "time_of_day": _time_of_day(now.hour),
        "weather_summary": weather_summary,
        "calendar_events": calendar_events,
    }
    brief: Brief = {"today_context": today_context}

    # ── Phase 2: parallel pitch (all agents, same Brief) ─────────────
    agent_map = {a.name: a for a in agents}

    with concurrent.futures.ThreadPoolExecutor() as pool:
        pitch_futures = {
            name: pool.submit(
                agent_map[name].pitch,
                brief,
                memories[name],
                contexts[name],
                user_id,
            )
            for name in contexts
        }
        pitches_by_agent: dict[str, list[Pitch]] = {
            name: f.result() for name, f in pitch_futures.items()
        }

    return pitches_by_agent, brief


# ── CLI ───────────────────────────────────────────────────────────────

def _build_default_agents() -> list[DataAgent]:
    from agents.calendar.agent import CalendarAgent
    from agents.weather.agent import WeatherAgent
    from agents.youtube.agent import YouTubeAgent

    return [WeatherAgent(), CalendarAgent(), YouTubeAgent()]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Run one episode generation pass and print EpisodeScript JSON."
    )
    parser.add_argument("--user-id", default="dev", help="User ID for seeding / memory lookup")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM passes (template hooks + no script)")
    args = parser.parse_args()

    print(f"[orchestrator] Running episode for user_id={args.user_id!r} …\n")

    # ── Phase 1+2: fetch + pitch ────────────────────────────────────
    if args.no_llm:
        os.environ["DISABLE_LLM"] = "1"

    agents = _build_default_agents()
    pitches_by_agent, brief = run_episode(agents, user_id=args.user_id)

    for agent_name, pitches in pitches_by_agent.items():
        print(f"── {agent_name} ({len(pitches)} pitch{'es' if len(pitches) != 1 else ''}) ──")
        for p in pitches:
            print(f"  [{p['priority']:.4f}] {p['title']}")
            print(f"          claim_kind={p['claim_kind']}  provenance={p['provenance_shape']}")
            print(f"          hook: {p['hook'][:90]}…" if len(p['hook']) > 90 else f"          hook: {p['hook']}")
        print()

    # ── Segment selection (deterministic) ───────────────────────────
    from producer.segments import select_segments

    selected = select_segments(pitches_by_agent)
    print(f"── Running order ({len(selected)} segments) ──")
    for i, seg in enumerate(selected):
        length = seg.get('suggested_length_sec', '?')
        print(f"  {i+1}. [{seg['agent']}] {seg['title']} ({length}s)")
    print()

    # ── Producer LLM pass ───────────────────────────────────────────
    if args.no_llm:
        print("[orchestrator] --no-llm: skipping Producer script generation.")
        print(json.dumps(selected, indent=2))
    else:
        from producer.script import generate_episode_script

        print("[orchestrator] Generating episode script via LLM …\n")
        try:
            episode = generate_episode_script(selected, brief)
            print(json.dumps(episode, indent=2))
        except Exception as e:
            print(f"[orchestrator] Producer LLM failed: {e}")
            print("[orchestrator] Falling back to raw segment JSON.")
            print(json.dumps(selected, indent=2))

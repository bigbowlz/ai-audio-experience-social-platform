"""Episode orchestrator: two-round pitch flow + Brief assembly + dispatch.

Flow (per producer/docs/DESIGN.md + docs/specs/2026-04-17-producer-alignment-plan.md):

  Round 1 — internal pitch round
      weather/calendar/youtube: fetch_context (parallel) → assemble Brief → pitch (parallel)

  Producer decision + marketplace + agentic payment (CLI-level orchestration)

  Round 2 — external pitch round (optional)
      external agents (e.g. AlicesAgent): fetch_context → pitch, reusing Brief

  agent.pitching.* events carry {"phase": "internal"|"external"}.

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
    internal_agents: list[DataAgent],
    external_agents: list[DataAgent] | None = None,
    user_id: str = "dev",
) -> tuple[dict[str, list[Pitch]], Brief]:
    """Run one episode generation pass — internal then external pitch round.

    The internal round runs first; Producer (in CLI/coordinator) decides
    to invoke external before the external round fires. This function
    runs both rounds and returns the merged pitch dict.

    Emits agent.pitching.* events with phase: "internal"|"external".

    Spec: docs/specs/2026-04-17-producer-alignment-plan.md Phase 2
          agents/docs/DESIGN.md §Reviewer Concern #2 (phase field)
    """
    from producer.events import emit

    external_agents = external_agents or []

    # ── Internal round ────────────────────────────────────────────────
    # Gate emits on non-empty agent list: CLI calls run_episode a second time
    # with internal_agents=[] for the external-only round, and a spurious
    # empty internal pitching pair would mislead SSE/phase-tag consumers.
    if internal_agents:
        emit("agent.pitching.started", {"phase": "internal"})
    pitches_by_agent, brief = _run_pitch_round(
        internal_agents, user_id, phase="internal"
    )
    if internal_agents:
        emit("agent.pitching.done", {"phase": "internal"})

    # ── External round (if any) ───────────────────────────────────────
    if external_agents:
        emit("agent.pitching.started", {"phase": "external"})
        external_pitches, _ = _run_pitch_round(
            external_agents, user_id, phase="external", brief=brief,
        )
        emit("agent.pitching.done", {"phase": "external"})
        pitches_by_agent.update(external_pitches)

    return pitches_by_agent, brief


def _run_pitch_round(
    agents: list[DataAgent],
    user_id: str,
    phase: str,
    brief: Brief | None = None,
) -> tuple[dict[str, list[Pitch]], Brief]:
    """One round of fetch_context (parallel) → assemble Brief → pitch (parallel).

    If `brief` is provided (external round), skip Brief assembly and reuse it.
    """
    _ = phase  # phase is emitted by the caller; helper keeps it for symmetry.

    with concurrent.futures.ThreadPoolExecutor() as pool:
        ctx_futures = {a.name: pool.submit(a.fetch_context, user_id) for a in agents}
        mem_futures = {a.name: pool.submit(a.load_memory, user_id) for a in agents}
        contexts: dict[str, ScopeContext] = {
            n: f.result() for n, f in ctx_futures.items()
        }
        memories: dict[str, AgentMemory] = {
            n: f.result() for n, f in mem_futures.items()
        }

    if brief is None:
        # Internal round: assemble Brief from weather + calendar contexts.
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
        brief = {"today_context": today_context}

    agent_map = {a.name: a for a in agents}
    with concurrent.futures.ThreadPoolExecutor() as pool:
        pitch_futures = {
            name: pool.submit(
                agent_map[name].pitch, brief, memories[name], contexts[name], user_id
            )
            for name in contexts
        }
        pitches_by_agent: dict[str, list[Pitch]] = {
            name: f.result() for name, f in pitch_futures.items()
        }

    return pitches_by_agent, brief


# ── CLI ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    from payment.stub import initiate_tx
    from producer.events import JsonlSink, emit, subscribe
    from producer.external import (
        decide_external_invocation,
        query_marketplace,
        select_external,
    )

    # Print every SSE-bound event as JSONL to stdout so `producer.external_decision.started`,
    # `producer.marketplace.queried`, `producer.external_agent.selected`, `payment.*`,
    # `producer.memory.applied`, `producer.selecting.*`, and `agent.pitching.*` all show up
    # in the CLI smoke test. The api-storage component will replace this sink with an HTTP/SSE one.
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
    args = parser.parse_args()

    if args.no_llm:
        os.environ["DISABLE_LLM"] = "1"

    print(f"[orchestrator] Running episode for user_id={args.user_id!r} …\n")

    # ── Internal pitch round ─────────────────────────────────────────
    from agents.calendar.agent import CalendarAgent
    from agents.weather.agent import WeatherAgent
    from agents.youtube.agent import YouTubeAgent

    internal_agents = [WeatherAgent(), CalendarAgent(), YouTubeAgent()]

    pitches_by_agent, brief = run_episode(internal_agents, user_id=args.user_id)

    # ── Producer external decision → marketplace → payment → external pitch ──
    if not args.no_external:
        decision = decide_external_invocation(pitches_by_agent)
        emit("producer.external_decision.started", {
            "reason": "anti_cocoon_policy_v0",
            "reasoning_summary": decision["rationale"],
        })
        if decision["decision"] == "invoke":
            candidates = query_marketplace()
            emit("producer.marketplace.queried", {
                "candidates": [
                    {"handle": c["handle"], "display_name": c["display_name"],
                     "price_usdc": c["price_usdc"]}
                    for c in candidates
                ],
                "reasoning_summary": f"{len(candidates)} candidates available",
            })
            chosen = select_external(candidates, brief=brief)
            emit("producer.external_agent.selected", {
                "agent": chosen["handle"],
                "display_name": chosen["display_name"],
                "rationale": "expands topic diversity for this brief",
                "reasoning_summary": f"picked {chosen['handle']}",
            })

            # ── Agentic payment (mocked) ────────────────────────────
            tx = initiate_tx(
                from_wallet="0xPRODUCER",
                to_wallet=chosen["wallet_address"],
                amount_usdc=chosen["price_usdc"],
            )
            emit("payment.initiated", {
                "to": chosen["wallet_address"],
                "amount_usdc": chosen["price_usdc"],
                "mode_badge": tx["mode"],
            })
            emit("payment.confirmed", {
                "tx_hash": tx["tx_hash"],
                "basescan_url": tx["basescan_url"],
                "mode_badge": tx["mode"],
            })

            # ── External pitch round ────────────────────────────────
            from agents.alices.agent import AlicesAgent

            external_pitches, _ = run_episode(
                internal_agents=[],
                external_agents=[AlicesAgent()],
                user_id=args.user_id,
            )
            pitches_by_agent.update(external_pitches)

    # ── Display per-agent pitches ────────────────────────────────────
    for agent_name, pitches in pitches_by_agent.items():
        print(f"── {agent_name} ({len(pitches)} pitch{'es' if len(pitches) != 1 else ''}) ──")
        for p in pitches:
            print(f"  [{p['priority']:.4f}] {p['title']}")
            print(f"          claim_kind={p['claim_kind']}  provenance={p['provenance_shape']}")
            hook_display = (
                f"          hook: {p['hook'][:90]}…"
                if len(p['hook']) > 90
                else f"          hook: {p['hook']}"
            )
            print(hook_display)
        print()

    # ── Step 0.5 + 1 + 1.5 + 2 (memory → guaranteed → bonus → script) ─
    from producer.bonus import select_bonus_with_events
    from producer.memory import (
        apply_producer_memory,
        emit_memory_applied,
        load_producer_memory,
    )
    from producer.segments import append_bonus, select_guaranteed_slots

    producer_memory = load_producer_memory(args.user_id)
    raw_pitches_by_agent = pitches_by_agent
    pitches_by_agent = apply_producer_memory(pitches_by_agent, producer_memory)
    emit_memory_applied(producer_memory, raw_pitches_by_agent, pitches_by_agent)

    order, remaining, bonus_budget = select_guaranteed_slots(pitches_by_agent)
    print(f"── Guaranteed slots ({order['guaranteed_count']}; {bonus_budget}s bonus budget) ──")
    for p in order["segments"]:
        print(f"  [{p['agent']}] {p['title']} ({p['suggested_length_sec']}s)")
    print()

    bonus, _guaranteed_reasons = select_bonus_with_events(
        guaranteed_slots=order["segments"],
        remaining_pitches=remaining,
        budget_remaining_sec=bonus_budget,
        today_context=brief["today_context"],
    )
    order = append_bonus(order, bonus)
    selected = order["segments"]
    print(f"── Bonus slots ({len(bonus)}) ──")
    for p in bonus:
        print(
            f"  [{p['agent']}] {p['title']} ({p['suggested_length_sec']}s) "
            f"— {p.get('reasoning_summary', '')}"
        )
    print()

    if args.no_llm:
        print("[orchestrator] --no-llm: skipping Producer script generation.")
        print(json.dumps(selected, indent=2))
    elif os.environ.get("ELEVENLABS_API_KEY"):
        import asyncio
        import time
        from pipeline import run_episode_pipeline

        print("[orchestrator] Running full pipeline (script + audio) …\n")
        try:
            episode_id = f"ep-{int(time.time())}"
            result = asyncio.run(run_episode_pipeline(selected, brief, episode_id))
            print(f"── Cold open ──\n{result.cold_open}\n")
            for seg_result in result.audio.segment_results:
                print(
                    f"  [segment {seg_result['segment_index']}] "
                    f"{seg_result['url']} ({seg_result['duration_ms']}ms)"
                )
            if result.audio.episode_failed is not None:
                print(f"\n[audio] episode failed: {result.audio.episode_failed.reason}")
            if result.audio.skipped_segments:
                print(f"\n[audio] skipped segments: {result.audio.skipped_segments}")
            print(f"\n── Sign-off ──\n{result.sign_off}\n")
        except Exception as e:
            print(f"[orchestrator] Pipeline failed: {e}")
            print("[orchestrator] Falling back to raw segment JSON.")
            print(json.dumps(selected, indent=2))
    else:
        from producer.script import generate_episode_script

        print("[orchestrator] No ELEVENLABS_API_KEY — script-only mode.\n")
        try:
            episode = generate_episode_script(selected, brief)
            print(json.dumps(episode, indent=2))
        except Exception as e:
            print(f"[orchestrator] Producer LLM failed: {e}")
            print("[orchestrator] Falling back to raw segment JSON.")
            print(json.dumps(selected, indent=2))

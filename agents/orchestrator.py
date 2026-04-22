"""Episode orchestrator: two-round pitch flow + Brief assembly + dispatch.

Flow (per producer/docs/DESIGN.md + docs/specs/2026-04-17-producer-alignment-plan.md):

  Round 1 — internal pitch round
      weather/calendar/youtube: fetch_context (parallel) → assemble Brief → pitch (parallel)

  Producer decision + marketplace + agentic payment (CLI-level orchestration)

  Round 2 — external pitch round (optional)
      external agents (e.g. ExternalAgent): fetch_context → pitch, reusing Brief

  agent.pitching.* events carry {"phase": "internal"|"external"}.

Usage (CLI):
    python -m agents.orchestrator
"""

from __future__ import annotations

import concurrent.futures
import json
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from agents.protocol import (
    AgentMemory,
    Brief,
    DataAgent,
    Pitch,
    ScopeContext,
    TodayContext,
    UserProfile,
)

# Module-level so tests can monkeypatch `agents.orchestrator.ensure_agent_auth`
# cleanly. No circular import risk — auth.preflight only does its (lazy)
# agent-module imports at call time.
from auth.preflight import ensure_agent_auth

# Same rationale — module-level binding so tests can monkeypatch
# `agents.orchestrator.hydrate_producer_memory` directly rather than
# relying on sys.modules side effects from patching the source module.
from learning_loop.seed_from_feedback import hydrate_producer_memory
from learning_loop.hydrate_topic_multipliers import hydrate_topic_multipliers

if TYPE_CHECKING:
    pass


# ── User profile loader ───────────────────────────────────────────────

_USER_PROFILE_PATH = Path.home() / ".config" / "radio-podcast" / "user_profile.json"


def _load_user_profile() -> UserProfile | None:
    """Return UserProfile from ~/.config/radio-podcast/user_profile.json, or None.

    Written once by auth/calendar_auth.py after OAuth consent. Absent file,
    parse error, or absent fields all degrade cleanly to None — the Producer
    falls back to addressing the user as "you".
    """
    if not _USER_PROFILE_PATH.exists():
        return None
    try:
        raw = json.loads(_USER_PROFILE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    profile: UserProfile = {}
    first_name = raw.get("first_name")
    display_name = raw.get("display_name")
    if isinstance(first_name, str) and first_name.strip():
        profile["first_name"] = first_name.strip()
    if isinstance(display_name, str) and display_name.strip():
        profile["display_name"] = display_name.strip()
    return profile or None


# ── Time-of-day helper ────────────────────────────────────────────────


def _time_of_day(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


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
        n
        for n, on in zip(
            _INTERNAL_AGENT_ORDER,
            (weather, calendar, youtube),
            strict=True,
        )
        if on
    ]
    if not names:
        raise SystemExit(
            "Select at least one agent: --weather, --calendar, --youtube "
            "(use --help for details)."
        )
    return names


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
            external_agents,
            user_id,
            phase="external",
            brief=brief,
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
            "now": now.strftime("%H:%M:%S"),
            "weather_summary": weather_summary,
            "calendar_events": calendar_events,
        }
        brief = {"today_context": today_context, "user_profile": _load_user_profile()}

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


def cli_main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns process exit code.

    argv=None → argparse reads sys.argv. Passing argv enables unit tests
    without monkeypatching sys.argv.
    """
    import argparse

    from payment.stub import initiate_tx
    from producer.events import PrettySink, emit, subscribe
    from producer.external import (
        decide_external_invocation,
        query_marketplace,
        select_external,
    )

    # Print every SSE-bound event as an indented key-per-line tree to stdout so
    # `producer.external_decision.started`, `producer.marketplace.queried`,
    # `producer.external_agent.selected`, `payment.*`, `producer.memory.applied`,
    # `producer.selecting.*`, and `agent.pitching.*` all surface readably in the
    # CLI smoke test. The api-storage component will swap this for an HTTP/SSE
    # JSONL sink.
    subscribe(PrettySink())

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
    parser.add_argument(
        "--weather",
        action="store_true",
        help="Activate the Weather agent (requires weather_location.json; "
        "auth flow runs automatically if missing)",
    )
    parser.add_argument(
        "--calendar",
        action="store_true",
        help="Activate the Calendar agent (requires Google OAuth; "
        "auth flow runs automatically if missing)",
    )
    parser.add_argument(
        "--youtube",
        action="store_true",
        help="Activate the YouTube agent (requires YouTube Data API OAuth; "
        "auth flow runs automatically if missing)",
    )
    parser.add_argument(
        "--no-export",
        action="store_true",
        help="Skip ffmpeg concat step at end of run (dev iteration).",
    )
    args = parser.parse_args(argv)

    if args.no_llm:
        os.environ["DISABLE_LLM"] = "1"

    from storage.episode_artifacts import (
        save_bonus,
        save_brief,
        save_episode_script,
        save_guaranteed,
        save_opener,
        save_pitches,
        save_running_order,
        save_segment,
        save_sign_off,
    )
    from storage.episode_dir import new_episode_id

    episode_id = new_episode_id()
    print(f"[orchestrator] Episode {episode_id} | user={args.user_id}\n")

    # ── Internal pitch round ─────────────────────────────────────────
    agent_names = _select_internal_agent_classes(
        weather=args.weather,
        calendar=args.calendar,
        youtube=args.youtube,
    )

    # Auth preflight BEFORE agent class imports — if an artifact is missing,
    # preflight runs the inline auth flow (browser + fallback URL to stdout)
    # and raises RuntimeError only if the flow failed to produce the artifact.
    # Running preflight here ensures a missing artifact surfaces as a clean
    # preflight error, not a constructor-time explosion inside the agent.
    # `ensure_agent_auth` is imported at module level for test monkeypatch
    # clarity — see agents/orchestrator.py top.
    for name in agent_names:
        ensure_agent_auth(name)

    hydrated_weights = hydrate_producer_memory(args.user_id)
    if hydrated_weights:
        weights_str = ", ".join(
            f"{a}={w:.2f}" for a, w in sorted(hydrated_weights.items())
        )
        print(f"[setup]  Learning hydration   {weights_str}")
    else:
        print(f"[setup]  Learning hydration   bootstrap (no prior signals)")

    hydrated_topics = hydrate_topic_multipliers(args.user_id)
    if hydrated_topics:
        topics_str = ", ".join(
            f"{a}({len(w)})" for a, w in sorted(hydrated_topics.items())
        )
        print(f"[setup]  Topic weights        {topics_str}")
    else:
        print(f"[setup]  Topic weights        none (no config/topic_weights.toml)")

    from agents.calendar.agent import CalendarAgent
    from agents.weather.agent import WeatherAgent
    from agents.youtube.agent import YouTubeAgent

    _CLASS_BY_NAME = {
        "weather": WeatherAgent,
        "calendar": CalendarAgent,
        "youtube": YouTubeAgent,
    }
    internal_agents = [_CLASS_BY_NAME[n]() for n in agent_names]

    pitches_by_agent, brief = run_episode(internal_agents, user_id=args.user_id)
    save_brief(episode_id, dict(brief))

    tc = brief["today_context"]
    cal_events = tc.get("calendar_events") or []
    cal_summary = f"{len(cal_events)} events" if cal_events else "0 events"
    wx_summary = tc.get("weather_summary") or "n/a"
    date_str = tc.get("date", "?")
    dow_str = (tc.get("day_of_week") or "?")[:3]
    tod_str = tc.get("time_of_day", "?")
    print(
        f"\n[1/8] Brief assembled      "
        f"date={date_str} {dow_str} {tod_str}, "
        f"weather={wx_summary}, calendar={cal_summary}"
    )

    # ── Producer external decision → marketplace → payment → external pitch ──
    external_line: str | None = None
    if not args.no_external:
        decision = decide_external_invocation(pitches_by_agent)
        emit(
            "producer.external_decision.started",
            {
                "reason": "anti_cocoon_policy_v0",
                "reasoning_summary": decision["rationale"],
            },
        )
        if decision["decision"] == "invoke":
            candidates = query_marketplace()
            emit(
                "producer.marketplace.queried",
                {
                    "candidates": [
                        {
                            "handle": c["handle"],
                            "display_name": c["display_name"],
                            "price_usdc": c["price_usdc"],
                        }
                        for c in candidates
                    ],
                    "reasoning_summary": f"{len(candidates)} candidates available",
                },
            )
            chosen = select_external(candidates, brief=brief)
            emit(
                "producer.external_agent.selected",
                {
                    "agent": chosen["handle"],
                    "display_name": chosen["display_name"],
                    "rationale": "expands topic diversity for this brief",
                    "reasoning_summary": f"picked {chosen['handle']}",
                },
            )

            # ── Agentic payment (mocked) ────────────────────────────
            tx = initiate_tx(
                from_wallet="0xPRODUCER",
                to_wallet=chosen["wallet_address"],
                amount_usdc=chosen["price_usdc"],
            )
            emit(
                "payment.initiated",
                {
                    "to": chosen["wallet_address"],
                    "amount_usdc": chosen["price_usdc"],
                    "mode_badge": tx["mode"],
                },
            )
            emit(
                "payment.confirmed",
                {
                    "tx_hash": tx["tx_hash"],
                    "basescan_url": tx["basescan_url"],
                    "mode_badge": tx["mode"],
                },
            )

            # ── External pitch round ────────────────────────────────
            from agents.external.agent import ExternalAgent

            external_pitches, _ = run_episode(
                internal_agents=[],
                external_agents=[ExternalAgent()],
                user_id=args.user_id,
            )
            pitches_by_agent.update(external_pitches)
            external_line = (
                f"invoked {chosen['handle']} "
                f"(${chosen['price_usdc']:.2f} USDC, tx={tx['tx_hash'][:10]}…)"
            )

    pitches_summary = "  ".join(
        f"{a}={len(ps)}" for a, ps in sorted(pitches_by_agent.items())
    )
    print(f"\n[2/8] Pitches collected    {pitches_summary}")
    if external_line:
        print(f"         External agent    {external_line}")

    save_pitches(episode_id, pitches_by_agent)

    # ── Step 0.5 + 1 + 1.5 (memory → guaranteed → bonus) ─
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
    save_pitches(episode_id, pitches_by_agent, post_memory=True)

    mem_weights = producer_memory.get("agent_weights", {}) if producer_memory else {}
    if mem_weights:
        mem_summary = ", ".join(f"{a}={w:.2f}" for a, w in sorted(mem_weights.items()))
    else:
        mem_summary = "identity transform (bootstrap)"
    print(f"\n[3/8] Producer memory      {mem_summary}")

    order, remaining, bonus_budget = select_guaranteed_slots(pitches_by_agent)
    save_guaranteed(episode_id, order["segments"], bonus_budget)
    print(
        f"\n[4/8] Guaranteed slots     "
        f"{order['guaranteed_count']} slots, {bonus_budget}s bonus budget"
    )

    bonus, guaranteed_reasons = select_bonus_with_events(
        guaranteed_slots=order["segments"],
        remaining_pitches=remaining,
        budget_remaining_sec=bonus_budget,
        today_context=brief["today_context"],
    )
    # Pull overall_reasoning from the selection call's SSE payload is awkward —
    # select_bonus_with_events already emitted. Re-derive from the outer call
    # instead by calling the lower-level function again just for the string is
    # wasteful; just record what we have.
    save_bonus(
        episode_id,
        bonus,
        guaranteed_reasons,
        overall_reasoning="(see producer.selecting.started SSE event)",
    )
    order = append_bonus(order, bonus)
    selected = order["segments"]
    save_running_order(episode_id, selected, order["guaranteed_count"])

    total_sec = sum(s["suggested_length_sec"] for s in selected)
    print(
        f"\n[5/8] Bonus selection      "
        f"{len(bonus)} bonus picked → {len(selected)} segments, {total_sec}s total"
    )
    from producer.script import split_opener_inputs

    opener_wx, opener_cal, content_after_opener = split_opener_inputs(selected)
    opener_sources = [p for p in (opener_wx, opener_cal) if p]
    if content_after_opener:
        opener_sources.append(content_after_opener[0])
    if opener_sources:
        opener_label_parts = [
            {"weather": "wx", "calendar": "cal"}.get(p["agent"], p["agent"])
            for p in opener_sources
        ]
        opener_secs = sum(p["suggested_length_sec"] for p in opener_sources)
        opener_label = f"[opener:{'+'.join(opener_label_parts)}/{opener_secs}s]"
        tail = "  ".join(
            f"[{p['agent']}/{p['suggested_length_sec']}s]"
            for p in content_after_opener[1:]
        )
        running_summary = f"{opener_label}  {tail}".rstrip()
    else:
        running_summary = "  ".join(
            f"[{s['agent']}/{s['suggested_length_sec']}s]" for s in selected
        )
    print(f"\n[6/8] Running order        {running_summary}")

    if args.no_llm:
        print("\n[7/8] Script generation    skipped (--no-llm)")
        print(f"\n[8/8] Audio export         skipped (--no-llm)")
        print(f"\nArtifacts: data/episodes/{episode_id}/")
        _print_learning_preview(args.user_id, hydrated_weights)
        return 0

    # ── Step 2: script generation (inline so we can capture artifacts) ──
    import asyncio
    from producer.script import (
        SegmentScript,
        build_opener_payload,
        build_segment_payload,
        build_sign_off_payload,
        generate_opener,
        generate_segment,
        generate_sign_off,
        split_opener_inputs,
    )

    try:
        weather_pitch, calendar_pitch, content_pitches = split_opener_inputs(selected)
        if not content_pitches:
            raise ValueError(
                "no content pitches after opener split "
                "(running order was weather/calendar only)"
            )

        async def _run_script() -> tuple[str, list[SegmentScript], str]:
            opener_payload = build_opener_payload(
                weather_pitch, calendar_pitch, content_pitches[0], brief
            )
            opener_text = await generate_opener(
                weather_pitch, calendar_pitch, content_pitches[0], brief
            )
            save_opener(episode_id, opener_payload, opener_text)

            segs: list[SegmentScript] = []
            for i, pitch in enumerate(content_pitches):
                is_first = i == 0
                previous_pitch = content_pitches[i - 1] if i > 0 else None
                seg_payload = build_segment_payload(
                    pitch, brief, is_first, previous_segment=previous_pitch
                )
                seg = await generate_segment(
                    pitch, brief, is_first=is_first, previous_segment=previous_pitch
                )
                save_segment(episode_id, i, seg_payload, dict(seg))
                segs.append(seg)

            so_payload = build_sign_off_payload(brief)
            so_text = await generate_sign_off(brief)
            save_sign_off(episode_id, so_payload, so_text)
            return opener_text, segs, so_text

        opener, content_segments, sign_off = asyncio.run(_run_script())

        episode_script = {
            "opener": opener,
            "segments": [dict(s) for s in content_segments],
            "sign_off": sign_off,
        }
        save_episode_script(episode_id, episode_script)

        seg_breakdown = "  ".join(
            f"seg{i+1} {s['estimated_length_sec']}s"
            for i, s in enumerate(content_segments)
        )
        print(
            f"\n[7/8] Script generation    "
            f"opener {len(opener.split())}w  {seg_breakdown}  "
            f"sign-off {len(sign_off.split())}w"
        )
    except Exception as e:
        print(f"\n[7/8] Script generation    FAILED: {e}")
        print(f"\nArtifacts: data/episodes/{episode_id}/")
        _print_learning_preview(args.user_id, hydrated_weights)
        return 1

    # ── Step 3: audio ──
    if not os.environ.get("ELEVENLABS_API_KEY"):
        print(f"\n[8/8] Audio export         skipped (no ELEVENLABS_API_KEY)")
        print(f"\nArtifacts: data/episodes/{episode_id}/")
        _print_learning_preview(args.user_id, hydrated_weights)
        return 0

    # Audio generation consumes content_segments, not the full EpisodeScript.
    # Replays the same content segments back through audio (cache-idempotent).
    try:
        from audio.orchestrator import generate_episode_audio
        from audio.tts import TTSClient

        tts = TTSClient(api_key=os.environ["ELEVENLABS_API_KEY"])

        async def _run_audio():
            from producer.script import SegmentScript as _SegScript

            # Full playback order: opener → content segments → sign_off.
            # agent="narrator" falls back to NARRATOR_VOICE_ID in audio.config.VOICE_MAP.
            all_segs = [
                _SegScript(
                    agent="narrator",
                    pitch_title="opener",
                    segue_in="",
                    script=opener,
                    estimated_length_sec=75,
                ),
                *content_segments,
                _SegScript(
                    agent="narrator",
                    pitch_title="sign_off",
                    segue_in="",
                    script=sign_off,
                    estimated_length_sec=12,
                ),
            ]

            async def _seg_iter():
                for s in all_segs:
                    yield s

            return await generate_episode_audio(tts, _seg_iter(), episode_id)

        audio_result = asyncio.run(_run_audio())

        mp3_count = len(audio_result.segment_results)
        print(
            f"\n[8/8] Audio export         "
            f"data/episodes/{episode_id}/ ({mp3_count} segments)"
        )

        # Auto-export concat MP3 unless --no-export.
        if not args.no_export:
            from storage.export import concat_episode_mp3

            try:
                out = concat_episode_mp3(episode_id)
                print(f"         Concat MP3         {out}")
            except RuntimeError as e:
                print(f"         Concat MP3         FAILED: {e}")

        # Playback (interactive).
        from dataclasses import asdict
        from player.cli_player import FeedbackSignal, play_episode
        from storage.signals import append_signal

        async def _on_feedback(sig: FeedbackSignal) -> None:
            append_signal(args.user_id, episode_id, asdict(sig))

        # Build index → (agent, title): 0=opener, 1..N=content, N+1=sign_off.
        # Explicit map avoids the stale selected[index] lookup that misaligned
        # when weather/calendar occupy the early slots of the running order.
        _index_map: dict[int, tuple[str, str]] = {0: ("narrator", "opener")}
        for _ci, _pitch in enumerate(content_pitches):
            _index_map[_ci + 1] = (_pitch["agent"], _pitch["title"])
        _index_map[len(content_pitches) + 1] = ("narrator", "sign_off")

        player_segments = [
            {
                "segment_index": sr["segment_index"],
                "agent": _index_map.get(sr["segment_index"], ("unknown", ""))[0],
                "pitch_title": _index_map.get(sr["segment_index"], ("unknown", ""))[1],
                "url": sr["audio_path"],
            }
            for sr in audio_result.segment_results
        ]

        print(f"\nArtifacts: data/episodes/{episode_id}/")
        print("\n── Playback ── (l=like  s=skip  r=repeat  p=pause  q=quit) ──")
        asyncio.run(
            play_episode(
                segments=player_segments,
                user_id=args.user_id,
                episode_id=episode_id,
                on_feedback=_on_feedback,
            )
        )
    except Exception as e:
        print(f"\n[8/8] Audio export         FAILED: {e}")
        print(f"\nArtifacts: data/episodes/{episode_id}/")

    _print_learning_preview(args.user_id, hydrated_weights)
    return 0


def _print_learning_preview(user_id: str, hydrated_weights: dict) -> None:
    """Summarize how the just-logged signals would re-seed ProducerMemory."""
    print("\n── Learning signals logged — next run will re-seed ProducerMemory ──")
    from learning_loop.seed_from_feedback import compute_weights
    from storage.signals import iter_signals

    post_run_weights = compute_weights(list(iter_signals(user_id)))
    if post_run_weights:
        print(f"[learning] next-run agent_weights:")
        for agent, w in sorted(post_run_weights.items()):
            prev = hydrated_weights.get(agent, 1.0)
            arrow = "↑" if w > prev else ("↓" if w < prev else "·")
            print(f"  {agent:<10} {prev:.3f} → {w:.3f}  {arrow}")
    else:
        print("[learning] no learning signals captured this episode.")


if __name__ == "__main__":
    raise SystemExit(cli_main())

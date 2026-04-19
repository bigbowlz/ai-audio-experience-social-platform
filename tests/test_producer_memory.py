"""Tests for producer/memory.py — ProducerMemory reader, writer, decay, SSE.

Coverage per producer/docs/DESIGN.md §Producer-memory learning rule (v0):

Reader (apply_producer_memory, pure function):
- default weights: empty memory → priorities unchanged
- single agent boosted → only that agent scales
- intra-agent order preserved (argmax unchanged, relative order unchanged)
- cross-agent bonus reorder (boosted agent outranks a higher raw-priority rival)
- clamped over MAX / under MIN
- malformed weights (negative, NaN, None, non-numeric) → clamp / fallback
- demoted agent still wins its guaranteed slot (pipeline integration)
- memory absent (missing agent_weights key)
- bootstrap identity

End-to-end pipeline (the product-visible claim):
- bonus-slot flip with DISABLE_LLM fallback

Writer (apply_signal, decay_agent_weights):
- saturation boundary (like × 10 clamps to MAX)
- apply_signal is pure (input not mutated; deterministic)
- apply_signal compounds on repeat (learning-loop must dedupe)
- unknown signal raises
- per-episode EMA decay pulls weights toward DEFAULT

SSE (build_memory_applied_event):
- returns None on empty weights
- returns event with pre/post max_priority when weights present
- fires-before-selecting invariant covered by orchestrator smoke
"""

from __future__ import annotations

import math
import os
from unittest.mock import MagicMock, patch

import pytest

from agents.protocol import Pitch, TodayContext
from producer.memory import (
    AGENT_WEIGHT_MAX,
    AGENT_WEIGHT_MIN,
    DEFAULT_AGENT_WEIGHT,
    EMA_DECAY_ALPHA,
    ProducerMemory,
    apply_producer_memory,
    apply_signal,
    bootstrap_producer_memory,
    build_memory_applied_event,
    decay_agent_weights,
    load_producer_memory,
)


# ── Fixtures ──────────────────────────────────────────────────────────


def _pitch(agent: str, title: str, priority: float, seg_len: int = 90) -> Pitch:
    return Pitch(
        agent=agent,
        title=title,
        hook="hook",
        source_refs=[],
        priority=priority,
        thin_signal=False,
        claim_kind="neutral",
        provenance_shape="balanced",
        suggested_length_sec=seg_len,
    )


def _mem(weights: dict[str, float]) -> ProducerMemory:
    return ProducerMemory(
        schema_version=1,
        agent_weights=dict(weights),
        updated_at="2026-04-17T00:00:00+00:00",
    )


def _priorities(pitches: list[Pitch]) -> list[float]:
    return [p["priority"] for p in pitches]


# ── Reader: apply_producer_memory ─────────────────────────────────────


class TestApplyReaderDefaults:
    def test_empty_weights_leaves_priorities_unchanged(self):
        pitches = {
            "youtube": [_pitch("youtube", "a", 0.9), _pitch("youtube", "b", 0.5)],
            "calendar": [_pitch("calendar", "c", 0.7)],
        }
        out = apply_producer_memory(pitches, _mem({}))
        assert _priorities(out["youtube"]) == [0.9, 0.5]
        assert _priorities(out["calendar"]) == [0.7]

    def test_missing_agent_weights_key_is_safe(self):
        # Memory missing the agent_weights key entirely — .get(...) defaults cleanly.
        memory = {"schema_version": 1, "updated_at": "2026-04-17T00:00:00+00:00"}
        pitches = {"youtube": [_pitch("youtube", "a", 0.9)]}
        out = apply_producer_memory(pitches, memory)  # type: ignore[arg-type]
        assert _priorities(out["youtube"]) == [0.9]

    def test_bootstrap_memory_is_identity(self):
        pitches = {
            "youtube": [_pitch("youtube", "a", 0.9)],
            "weather": [_pitch("weather", "b", 0.6)],
        }
        out = apply_producer_memory(pitches, bootstrap_producer_memory())
        assert _priorities(out["youtube"]) == [0.9]
        assert _priorities(out["weather"]) == [0.6]

    def test_pure_function_does_not_mutate_input(self):
        original = _pitch("youtube", "a", 0.9)
        pitches = {"youtube": [original]}
        memory = _mem({"youtube": 1.5})
        out = apply_producer_memory(pitches, memory)
        # Input pitch untouched.
        assert original["priority"] == 0.9
        # Output is a different object with scaled priority.
        assert out["youtube"][0] is not original
        assert out["youtube"][0]["priority"] == pytest.approx(1.35)


class TestApplyReaderScaling:
    def test_single_agent_boosted_scales_only_that_agent(self):
        pitches = {
            "youtube": [_pitch("youtube", "a", 0.8)],
            "calendar": [_pitch("calendar", "b", 0.6)],
        }
        out = apply_producer_memory(pitches, _mem({"youtube": 1.5}))
        assert _priorities(out["youtube"]) == pytest.approx([1.2])
        assert _priorities(out["calendar"]) == pytest.approx([0.6])

    def test_intra_agent_order_preserved(self):
        pitches = {
            "youtube": [
                _pitch("youtube", "a", 0.9),
                _pitch("youtube", "b", 0.7),
                _pitch("youtube", "c", 0.5),
            ]
        }
        out = apply_producer_memory(pitches, _mem({"youtube": 1.5}))
        priorities = _priorities(out["youtube"])
        assert priorities == pytest.approx([1.35, 1.05, 0.75])
        # Order preserved, argmax preserved.
        assert priorities == sorted(priorities, reverse=True)

    def test_cross_agent_bonus_reorder(self):
        # Before: alices 0.7 > youtube 0.5. After ×1.5 youtube: 0.75 > 0.7.
        pitches = {
            "youtube": [_pitch("youtube", "y", 0.5)],
            "alices": [_pitch("alices", "p", 0.7)],
        }
        out = apply_producer_memory(
            pitches, _mem({"youtube": 1.5, "alices": 1.0})
        )
        assert out["youtube"][0]["priority"] == pytest.approx(0.75)
        assert out["alices"][0]["priority"] == pytest.approx(0.7)
        assert out["youtube"][0]["priority"] > out["alices"][0]["priority"]


class TestApplyReaderClamping:
    def test_weight_clamped_over_max(self):
        pitches = {"youtube": [_pitch("youtube", "a", 0.8)]}
        out = apply_producer_memory(pitches, _mem({"youtube": 5.0}))
        # Effective weight = AGENT_WEIGHT_MAX (2.0); priority = 0.8 * 2.0.
        assert out["youtube"][0]["priority"] == pytest.approx(0.8 * AGENT_WEIGHT_MAX)

    def test_weight_clamped_under_min(self):
        pitches = {"calendar": [_pitch("calendar", "c", 0.8)]}
        out = apply_producer_memory(pitches, _mem({"calendar": 0.01}))
        assert out["calendar"][0]["priority"] == pytest.approx(0.8 * AGENT_WEIGHT_MIN)

    def test_negative_weight_clamped_to_min(self):
        pitches = {"youtube": [_pitch("youtube", "a", 0.8)]}
        out = apply_producer_memory(pitches, _mem({"youtube": -0.5}))
        # Negative → clamped to MIN, not propagated.
        assert out["youtube"][0]["priority"] == pytest.approx(0.8 * AGENT_WEIGHT_MIN)

    def test_nan_weight_falls_back_to_default(self):
        pitches = {"weather": [_pitch("weather", "w", 0.8)]}
        out = apply_producer_memory(pitches, _mem({"weather": float("nan")}))
        # NaN is garbage; treat as default (unscaled).
        assert out["weather"][0]["priority"] == pytest.approx(0.8)

    def test_inf_weight_falls_back_to_default(self):
        pitches = {"youtube": [_pitch("youtube", "a", 0.8)]}
        out = apply_producer_memory(pitches, _mem({"youtube": float("inf")}))
        assert out["youtube"][0]["priority"] == pytest.approx(0.8)

    def test_none_weight_falls_back_to_default(self):
        pitches = {"calendar": [_pitch("calendar", "c", 0.8)]}
        # None stored value (e.g. from a bad DB roundtrip) → default 1.0.
        out = apply_producer_memory(pitches, _mem({"calendar": None}))  # type: ignore[dict-item]
        assert out["calendar"][0]["priority"] == pytest.approx(0.8)


class TestApplyReaderPipelineIntegration:
    def test_demoted_agent_still_wins_its_guaranteed_slot(self):
        """Guaranteed slot is structural — weight only affects bonus competition."""
        from producer.segments import select_guaranteed_slots

        pitches = {
            "calendar": [_pitch("calendar", "standup", 0.5, seg_len=30)],
            "youtube": [_pitch("youtube", "jazz", 0.9, seg_len=90)],
        }
        adjusted = apply_producer_memory(pitches, _mem({"calendar": 0.3}))
        # Calendar's only pitch scales to 0.15 but still claims its guaranteed slot.
        order, remaining, _ = select_guaranteed_slots(adjusted)
        guaranteed = order["segments"]
        agents = {p["agent"] for p in guaranteed}
        assert agents == {"calendar", "youtube"}
        calendar_slot = next(p for p in guaranteed if p["agent"] == "calendar")
        assert calendar_slot["priority"] == pytest.approx(0.15)


class TestPipelineBonusSlotFlip:
    """End-to-end: apply_producer_memory → select_guaranteed → select_bonus_segments_llm.

    The product-visible claim — a weighted agent can flip a bonus-slot winner.
    Uses DISABLE_LLM=1 to force the deterministic priority-sort fallback path
    so no network/mocks are required. `length_overrides` is passed to
    `select_guaranteed_slots` so the test fully controls costs (the default
    lookup in producer/segments.py would otherwise ignore per-pitch seg_lens).
    """

    _TODAY: TodayContext = {
        "date": "2026-04-17",
        "day_of_week": "Thursday",
        "time_of_day": "morning",
        "weather_summary": None,
        "calendar_events": None,
    }

    def _run_pipeline(
        self,
        pitches_by_agent: dict[str, list[Pitch]],
        memory: ProducerMemory,
        length_overrides: dict[str, int],
        budget_override: int,
    ) -> list[Pitch]:
        from producer.bonus import select_bonus_segments_llm
        from producer.segments import select_guaranteed_slots

        adjusted = apply_producer_memory(pitches_by_agent, memory)
        order, remaining, _ = select_guaranteed_slots(
            adjusted, length_overrides=length_overrides
        )
        guaranteed = order["segments"]
        with patch.dict(os.environ, {"DISABLE_LLM": "1"}):
            bonus, _, _ = select_bonus_segments_llm(
                guaranteed_slots=guaranteed,
                remaining_pitches=remaining,
                budget_remaining_sec=budget_override,
                today_context=self._TODAY,
            )
        return guaranteed + bonus

    def test_boost_flips_bonus_slot_winner(self):
        # 2 agents × 2 pitches, all segs forced to 40s so each bonus pitch costs 50
        # (40 + 10 segue). Budget = 50 → exactly ONE bonus slot fits.
        #
        # Raw #2 priorities: alices 0.80 > youtube 0.70.
        # Without weights, alices' #2 pitch wins the single bonus slot.
        pitches = {
            "youtube": [
                _pitch("youtube", "yt-1", 0.95),
                _pitch("youtube", "yt-2", 0.70),
            ],
            "alices": [
                _pitch("alices", "p-1", 0.92),
                _pitch("alices", "p-2", 0.80),
            ],
        }
        overrides = {"youtube": 40, "alices": 40}

        neutral = self._run_pipeline(
            pitches, _mem({}), length_overrides=overrides, budget_override=50
        )
        bonus_agents_neutral = [p["agent"] for p in neutral[2:]]  # skip 2 guaranteed
        assert bonus_agents_neutral == ["alices"]

        # Boost youtube ×1.5 → yt-2 effective priority 1.05, beats alices 0.80.
        boosted = self._run_pipeline(
            pitches,
            _mem({"youtube": 1.5}),
            length_overrides=overrides,
            budget_override=50,
        )
        bonus_agents_boosted = [p["agent"] for p in boosted[2:]]
        assert bonus_agents_boosted == ["youtube"]

        # Guaranteed slots unchanged (structural invariant).
        guaranteed_agents_neutral = {p["agent"] for p in neutral[:2]}
        guaranteed_agents_boosted = {p["agent"] for p in boosted[:2]}
        assert guaranteed_agents_neutral == guaranteed_agents_boosted == {
            "youtube", "alices"
        }


# ── Writer: apply_signal ──────────────────────────────────────────────


class TestApplySignal:
    def test_like_increases_weight(self):
        mem = _mem({})
        after = apply_signal(mem, "youtube", "like")
        assert after["agent_weights"]["youtube"] == pytest.approx(1.10)

    def test_skip_decreases_weight(self):
        mem = _mem({})
        after = apply_signal(mem, "youtube", "skip")
        assert after["agent_weights"]["youtube"] == pytest.approx(0.90)

    def test_replay_beats_like(self):
        like = apply_signal(_mem({}), "youtube", "like")
        replay = apply_signal(_mem({}), "youtube", "replay")
        assert replay["agent_weights"]["youtube"] > like["agent_weights"]["youtube"]

    def test_unknown_signal_raises(self):
        with pytest.raises(ValueError, match="Unknown signal"):
            apply_signal(_mem({}), "youtube", "hearted")

    def test_pure_function_does_not_mutate_input(self):
        mem = _mem({"youtube": 1.0})
        _ = apply_signal(mem, "youtube", "like")
        # Input untouched.
        assert mem["agent_weights"]["youtube"] == 1.0

    def test_deterministic(self):
        mem = _mem({"youtube": 1.0})
        a = apply_signal(mem, "youtube", "like")
        b = apply_signal(mem, "youtube", "like")
        assert a["agent_weights"]["youtube"] == b["agent_weights"]["youtube"]

    def test_compounds_on_repeat_learning_loop_must_dedupe(self):
        """Signal function is stateless; callers dedupe by (episode, segment, signal)."""
        mem = _mem({})
        once = apply_signal(mem, "youtube", "like")
        twice = apply_signal(once, "youtube", "like")
        assert twice["agent_weights"]["youtube"] > once["agent_weights"]["youtube"]
        assert twice["agent_weights"]["youtube"] == pytest.approx(1.10 * 1.10)

    def test_saturation_boundary_ten_likes_clamps_to_max(self):
        mem = _mem({})
        for _ in range(10):
            mem = apply_signal(mem, "youtube", "like")
        assert mem["agent_weights"]["youtube"] == pytest.approx(AGENT_WEIGHT_MAX)

    def test_saturation_lower_ten_skips_clamps_to_min(self):
        mem = _mem({"youtube": AGENT_WEIGHT_MAX})
        for _ in range(20):
            mem = apply_signal(mem, "youtube", "skip")
        assert mem["agent_weights"]["youtube"] == pytest.approx(AGENT_WEIGHT_MIN)


# ── Writer: decay_agent_weights ───────────────────────────────────────


class TestDecay:
    def test_decay_pulls_weights_toward_default(self):
        mem = _mem({"youtube": 2.0, "calendar": 0.3})
        after = decay_agent_weights(mem)
        # 0.95 * 2.0 + 0.05 * 1.0 = 1.95
        assert after["agent_weights"]["youtube"] == pytest.approx(1.95)
        # 0.95 * 0.3 + 0.05 * 1.0 = 0.335
        assert after["agent_weights"]["calendar"] == pytest.approx(0.335)

    def test_decay_of_default_is_default(self):
        mem = _mem({"youtube": DEFAULT_AGENT_WEIGHT})
        after = decay_agent_weights(mem)
        assert after["agent_weights"]["youtube"] == pytest.approx(DEFAULT_AGENT_WEIGHT)

    def test_decay_does_not_mutate_input(self):
        mem = _mem({"youtube": 2.0})
        _ = decay_agent_weights(mem)
        assert mem["agent_weights"]["youtube"] == 2.0

    def test_decay_uses_alpha_constant(self):
        """Alpha moves weight 5% toward default per episode."""
        mem = _mem({"youtube": 2.0})
        after = decay_agent_weights(mem)
        expected = (1 - EMA_DECAY_ALPHA) * 2.0 + EMA_DECAY_ALPHA * DEFAULT_AGENT_WEIGHT
        assert after["agent_weights"]["youtube"] == pytest.approx(expected)

    def test_decay_converges_toward_default_over_many_episodes(self):
        """After many decays with no reinforcement, weight approaches default."""
        mem = _mem({"youtube": 2.0})
        for _ in range(200):
            mem = decay_agent_weights(mem)
        assert mem["agent_weights"]["youtube"] == pytest.approx(
            DEFAULT_AGENT_WEIGHT, abs=1e-3
        )


# ── SSE: build_memory_applied_event ───────────────────────────────────


class TestBuildMemoryAppliedEvent:
    def test_returns_none_when_weights_empty(self):
        raw = {"youtube": [_pitch("youtube", "a", 0.9)]}
        adj = {"youtube": [_pitch("youtube", "a", 0.9)]}
        event = build_memory_applied_event(bootstrap_producer_memory(), raw, adj)
        assert event is None

    def test_emits_event_with_pre_post_max_priority(self):
        raw = {
            "youtube": [_pitch("youtube", "a", 0.9), _pitch("youtube", "b", 0.5)],
            "calendar": [_pitch("calendar", "c", 0.7)],
        }
        adj = apply_producer_memory(
            raw, _mem({"youtube": 1.5, "calendar": 0.8})
        )
        event = build_memory_applied_event(
            _mem({"youtube": 1.5, "calendar": 0.8}), raw, adj
        )
        assert event is not None
        assert event["agent_weights"] == {"youtube": 1.5, "calendar": 0.8}
        changes_by_agent = {c["agent"]: c for c in event["changes"]}
        assert changes_by_agent["youtube"]["pre_max_priority"] == pytest.approx(0.9)
        assert changes_by_agent["youtube"]["post_max_priority"] == pytest.approx(1.35)
        assert changes_by_agent["calendar"]["pre_max_priority"] == pytest.approx(0.7)
        assert changes_by_agent["calendar"]["post_max_priority"] == pytest.approx(0.56)

    def test_skips_agents_missing_from_pitches(self):
        """Weights for an agent without any pitches this episode → skipped in changes."""
        raw = {"youtube": [_pitch("youtube", "a", 0.9)]}
        adj = apply_producer_memory(raw, _mem({"youtube": 1.5, "alices": 0.8}))
        event = build_memory_applied_event(
            _mem({"youtube": 1.5, "alices": 0.8}), raw, adj
        )
        assert event is not None
        change_agents = {c["agent"] for c in event["changes"]}
        assert change_agents == {"youtube"}
        # agent_weights still includes alices for UI reconciliation.
        assert "alices" in event["agent_weights"]

    def test_emit_memory_applied_silent_when_weights_empty(self):
        """Bootstrap users emit nothing — silent identity transform."""
        from producer.events import EventBus, set_default_bus
        bus = EventBus()
        captured = []
        bus.subscribe(lambda n, p: captured.append((n, p)))
        set_default_bus(bus)
        raw = {"youtube": [_pitch("youtube", "a", 0.9)]}
        adj = raw
        from producer.memory import emit_memory_applied
        emit_memory_applied(bootstrap_producer_memory(), raw, adj)
        assert captured == []

    def test_emit_memory_applied_fires_when_weights_present(self):
        from producer.events import EventBus, set_default_bus
        bus = EventBus()
        captured = []
        bus.subscribe(lambda n, p: captured.append((n, p)))
        set_default_bus(bus)
        raw = {"youtube": [_pitch("youtube", "a", 0.9)]}
        adj = apply_producer_memory(raw, _mem({"youtube": 1.5}))
        from producer.memory import emit_memory_applied
        emit_memory_applied(_mem({"youtube": 1.5}), raw, adj)
        assert len(captured) == 1
        assert captured[0][0] == "producer.memory.applied"
        assert captured[0][1]["agent_weights"] == {"youtube": 1.5}


# ── Bootstrap + load ──────────────────────────────────────────────────


class TestBootstrap:
    def test_bootstrap_has_required_fields(self):
        mem = bootstrap_producer_memory()
        assert mem["schema_version"] == 1
        assert mem["agent_weights"] == {}
        assert mem["updated_at"]  # non-empty ISO string

    def test_load_producer_memory_returns_valid_shape(self):
        mem = load_producer_memory("dev-user")
        assert mem["schema_version"] == 1
        assert isinstance(mem["agent_weights"], dict)

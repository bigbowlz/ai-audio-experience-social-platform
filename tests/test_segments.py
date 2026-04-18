"""Tests for producer/segments.py — select_guaranteed_slots().

Coverage per prompt_design.md §Test mandate (Step 1):
- Phase 1 guaranteed slots (one per agent, highest priority wins)
- Thin-signal pitch handling (still gets its guaranteed slot)
- MAX_SEGMENT_SEC clamping
- DEFAULT_SEGMENT_SEC applied when no overrides
- length_overrides respected when provided
- Remaining pitches carry suggested_length_sec
- Budget math (open/close, segment lengths, N-1 segues)

Bonus/priority-sort coverage lives in tests/test_bonus_selection.py (fallback path).
"""

from __future__ import annotations

from agents.protocol import Pitch
from producer.segments import (
    DEFAULT_SEGMENT_SEC,
    MAX_SEGMENT_SEC,
    OPEN_CLOSE_SECS,
    SEGUE_OVERHEAD_SECS,
    TARGET_EPISODE_SECS,
    select_guaranteed_slots,
)


def _pitch(agent: str, priority: float, thin_signal: bool = False, title: str | None = None) -> Pitch:
    return Pitch(
        agent=agent,
        title=title or f"{agent} pitch p{priority}",
        hook="hook",
        rationale="test",
        source_refs=[],
        priority=priority,
        thin_signal=thin_signal,
        claim_kind="neutral",
        provenance_shape="balanced",
    )


class TestPhase1GuaranteedSlots:
    def test_each_agent_gets_one_slot(self):
        pitches = {
            "youtube": [_pitch("youtube", 0.9), _pitch("youtube", 0.5)],
            "weather": [_pitch("weather", 0.3)],
            "calendar": [_pitch("calendar", 0.4)],
            "alices": [_pitch("alices", 0.7)],
        }
        order, _, _ = select_guaranteed_slots(pitches)
        guaranteed = order["segments"]
        agents = [p["agent"] for p in guaranteed]
        assert set(agents) == {"youtube", "weather", "calendar", "alices"}
        assert len(guaranteed) == 4

    def test_highest_priority_selected(self):
        pitches = {
            "youtube": [
                _pitch("youtube", 0.9, title="top"),
                _pitch("youtube", 0.5),
                _pitch("youtube", 0.3),
            ],
        }
        order, _, _ = select_guaranteed_slots(pitches)
        guaranteed = order["segments"]
        assert guaranteed[0]["title"] == "top"
        assert guaranteed[0]["priority"] == 0.9

    def test_remaining_carries_non_selected(self):
        pitches = {
            "youtube": [
                _pitch("youtube", 0.9, title="top"),
                _pitch("youtube", 0.5, title="mid"),
                _pitch("youtube", 0.3, title="low"),
            ],
        }
        _, remaining, _ = select_guaranteed_slots(pitches)
        titles = {p["title"] for p in remaining}
        assert titles == {"mid", "low"}

    def test_remaining_has_suggested_length_sec(self):
        pitches = {
            "youtube": [
                _pitch("youtube", 0.9),
                _pitch("youtube", 0.5),
            ],
        }
        _, remaining, _ = select_guaranteed_slots(pitches)
        assert len(remaining) == 1
        assert remaining[0]["suggested_length_sec"] == DEFAULT_SEGMENT_SEC["youtube"]

    def test_select_guaranteed_returns_running_order_shape(self):
        pitches = {"youtube": [_pitch("youtube", 0.9, title="a")]}
        # Override length to make total_sec deterministic.
        order, _, _ = select_guaranteed_slots(pitches, length_overrides={"youtube": 90})
        assert order["guaranteed_count"] == 1
        assert order["bonus_count"] == 0
        assert order["total_sec"] == 90
        assert order["segments"][0]["title"] == "a"


class TestThinSignalPitch:
    def test_thin_signal_gets_guaranteed_slot(self):
        pitches = {
            "youtube": [_pitch("youtube", 0.3, thin_signal=True)],
            "weather": [_pitch("weather", 0.5)],
        }
        order, _, _ = select_guaranteed_slots(pitches)
        guaranteed = order["segments"]
        yt = next(p for p in guaranteed if p["agent"] == "youtube")
        assert yt["thin_signal"] is True


class TestSegmentLengths:
    def test_default_lengths_applied(self):
        pitches = {
            "youtube": [_pitch("youtube", 0.9)],
            "weather": [_pitch("weather", 0.5)],
            "calendar": [_pitch("calendar", 0.4)],
            "alices": [_pitch("alices", 0.7)],
        }
        order, _, _ = select_guaranteed_slots(pitches)
        guaranteed = order["segments"]
        lengths = {p["agent"]: p["suggested_length_sec"] for p in guaranteed}
        assert lengths == DEFAULT_SEGMENT_SEC

    def test_overrides_respected(self):
        pitches = {"youtube": [_pitch("youtube", 0.9)]}
        order, _, _ = select_guaranteed_slots(pitches, length_overrides={"youtube": 60})
        assert order["segments"][0]["suggested_length_sec"] == 60

    def test_over_max_is_clamped(self):
        pitches = {"youtube": [_pitch("youtube", 0.9)]}
        order, _, _ = select_guaranteed_slots(pitches, length_overrides={"youtube": 200})
        assert order["segments"][0]["suggested_length_sec"] == MAX_SEGMENT_SEC


class TestBudgetMath:
    def test_single_agent_no_segue(self):
        """1 guaranteed segment → 0 segues. budget = 450 - 25 - 50 = 375."""
        pitches = {"a": [_pitch("a", 0.9)]}
        _, _, budget = select_guaranteed_slots(pitches, length_overrides={"a": 50})
        assert budget == TARGET_EPISODE_SECS - OPEN_CLOSE_SECS - 50

    def test_n_minus_1_segues(self):
        """2 guaranteed segments → 1 segue."""
        pitches = {
            "a": [_pitch("a", 0.9)],
            "b": [_pitch("b", 0.8)],
        }
        _, _, budget = select_guaranteed_slots(
            pitches, length_overrides={"a": 50, "b": 50}
        )
        # 450 - 25 - 50 - 50 - 1*10 = 315
        assert budget == TARGET_EPISODE_SECS - OPEN_CLOSE_SECS - 100 - SEGUE_OVERHEAD_SECS

    def test_budget_can_go_negative(self):
        """Guaranteed slots consume budget even if it blows through zero."""
        pitches = {
            "a": [_pitch("a", 0.9)],
            "b": [_pitch("b", 0.8)],
            "c": [_pitch("c", 0.7)],
            "d": [_pitch("d", 0.6)],
            "e": [_pitch("e", 0.5)],
        }
        # 5 segments at 90s each + 4 segues × 10s = 490, but 425 budget → -65
        _, _, budget = select_guaranteed_slots(
            pitches, length_overrides={k: 90 for k in "abcde"}
        )
        assert budget < 0


class TestTieBreakDeterminism:
    """Decision 5a: ties resolve by (-priority, agent, title) — reproducible."""

    @staticmethod
    def _p(agent: str, title: str, priority: float) -> Pitch:
        # Local helper — file-level `_pitch` has a different signature
        # (agent, priority, thin_signal, title). Keep this scoped so existing
        # call sites remain untouched.
        return Pitch(
            agent=agent,
            title=title,
            hook="hook",
            rationale="test",
            source_refs=[],
            priority=priority,
            thin_signal=False,
            claim_kind="neutral",
            provenance_shape="balanced",
        )

    def test_same_priority_within_agent_resolves_by_title_asc(self):
        # Two pitches at identical priority within one agent.
        # DESIGN: deterministic by title ASC as final tiebreaker.
        pitches = {
            "youtube": [
                self._p("youtube", "zebra", 0.9),
                self._p("youtube", "alpha", 0.9),  # same priority, alpha < zebra
            ],
        }
        order, _, _ = select_guaranteed_slots(pitches)
        assert order["segments"][0]["title"] == "alpha"

    def test_same_priority_across_agents_resolves_by_agent_asc(self):
        # Two agents with their top pitch tied — agent name ASC wins.
        pitches = {
            "youtube": [self._p("youtube", "y", 0.7)],
            "calendar": [self._p("calendar", "c", 0.7)],
        }
        order, _, _ = select_guaranteed_slots(pitches)
        # Both win their guaranteed slots (one per agent), but iteration
        # order in `segments` should be deterministic by agent ASC.
        agents = [p["agent"] for p in order["segments"]]
        assert agents == sorted(agents)  # ["calendar", "youtube"]

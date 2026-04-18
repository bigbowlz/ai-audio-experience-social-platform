"""Producer memory — inter-agent weights applied deterministically.

Per producer/docs/DESIGN.md §Producer-memory learning rule (v0):

    raw pitches_by_agent
      → apply_producer_memory(pitches, memory)   # pure function; no I/O, no LLM
      → adjusted pitches_by_agent                # priority scaled by agent_weights
      → select_guaranteed_slots(adjusted)
      → select_bonus_segments_llm(adjusted, …)   # LLM sees adjusted scalars only

The LLM never receives `producer_memory` as free-form input. Memory-driven
behavior is expressed as a pure transform on `priority`; the selection LLM
only sees the resulting numbers.

Scope boundary: ProducerMemory holds INTER-agent weights only.
Intra-agent signals (topic-level preferences) live in AgentMemory and are
owned by the agent — never surfaced here. See Reviewer Concern #4.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import TypedDict

from agents.protocol import Pitch
from producer.events import emit


# ── Constants ─────────────────────────────────────────────────────────

AGENT_WEIGHT_MIN = 0.3
AGENT_WEIGHT_MAX = 2.0
DEFAULT_AGENT_WEIGHT = 1.0

# Multiplicative update rules per feedback signal. Applied by learning-loop
# after each per-segment event (like/replay/skip). Clamped after each apply.
SIGNAL_MULTIPLIERS: dict[str, float] = {
    "like": 1.10,
    "replay": 1.20,
    "skip": 0.90,
}

# Per-episode pull toward DEFAULT_AGENT_WEIGHT. Half-life ≈ 13.5 episodes
# (log(0.5) / log(0.95)). Prevents stale weights from persisting after a
# user's interest has drifted.
EMA_DECAY_ALPHA = 0.05


# ── Shape ─────────────────────────────────────────────────────────────


class ProducerMemory(TypedDict):
    schema_version: int                  # = 1 for v0
    agent_weights: dict[str, float]      # agent_name → priority multiplier
    updated_at: str                      # ISO 8601


def bootstrap_producer_memory() -> ProducerMemory:
    """Default ProducerMemory for a user that has never had a row.

    Mirrors bootstrap_memory() in agents/protocol.py. Lazy-created in memory
    so the orchestrator can proceed without a DB round-trip; the learning-loop
    persists on first real write.
    """
    now = datetime.now(timezone.utc).isoformat()
    return ProducerMemory(schema_version=1, agent_weights={}, updated_at=now)


def load_producer_memory(user_id: str) -> ProducerMemory:
    """Return ProducerMemory for a user, bootstrapping when no row exists.

    v0 stub: always bootstraps. Persistence is a learning-loop-session
    concern; when wired, this reads from storage and falls back to
    bootstrap_producer_memory().
    """
    _ = user_id
    return bootstrap_producer_memory()


# ── Helpers ───────────────────────────────────────────────────────────


def _resolve_weight(raw: object) -> float:
    """Return an in-range weight from a raw stored value.

    Treats NaN, +/-inf, None, and non-numeric values as DEFAULT_AGENT_WEIGHT.
    Finite numbers are clamped to [AGENT_WEIGHT_MIN, AGENT_WEIGHT_MAX].
    Protects downstream math from malformed persistence.
    """
    if raw is None or isinstance(raw, bool):
        return DEFAULT_AGENT_WEIGHT
    try:
        w = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_AGENT_WEIGHT
    if math.isnan(w) or math.isinf(w):
        return DEFAULT_AGENT_WEIGHT
    return max(AGENT_WEIGHT_MIN, min(AGENT_WEIGHT_MAX, w))


# ── Reader: pure function applied pre-selection ───────────────────────


def apply_producer_memory(
    pitches_by_agent: dict[str, list[Pitch]],
    memory: ProducerMemory,
) -> dict[str, list[Pitch]]:
    """Scale each pitch's priority by its agent's ProducerMemory weight.

    Pure function — no I/O, no LLM, no mutation of inputs. Returns a new
    dict with new Pitch objects (priority scaled; all other fields copied).

    Invariants:
      - Within an agent, relative pitch order is preserved (all pitches
        scaled by the same weight → argmax unchanged).
      - Cross-agent bonus-slot competitiveness shifts with weight.
      - Guaranteed-slot invariant unaffected (structural, one per agent).
    """
    weights = memory.get("agent_weights", {})
    adjusted: dict[str, list[Pitch]] = {}
    for agent, pitches in pitches_by_agent.items():
        w = _resolve_weight(weights.get(agent, DEFAULT_AGENT_WEIGHT))
        adjusted[agent] = [{**p, "priority": p["priority"] * w} for p in pitches]
    return adjusted


# ── Writer primitives (learning-loop calls these) ─────────────────────


def apply_signal(
    memory: ProducerMemory,
    agent: str,
    signal: str,
) -> ProducerMemory:
    """Apply one feedback signal to the agent's weight (clamped).

    Pure function — returns new memory; does NOT mutate input. Dedupe of
    (episode_id, segment_index, signal) is the caller's responsibility;
    this function is stateless.
    """
    if signal not in SIGNAL_MULTIPLIERS:
        raise ValueError(f"Unknown signal: {signal!r}")
    weights = dict(memory.get("agent_weights", {}))
    current = _resolve_weight(weights.get(agent, DEFAULT_AGENT_WEIGHT))
    updated = current * SIGNAL_MULTIPLIERS[signal]
    weights[agent] = max(AGENT_WEIGHT_MIN, min(AGENT_WEIGHT_MAX, updated))
    return {
        **memory,
        "agent_weights": weights,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def decay_agent_weights(memory: ProducerMemory) -> ProducerMemory:
    """End-of-episode decay — pull every weight toward DEFAULT_AGENT_WEIGHT.

    Applied AFTER the episode's feedback signals have landed. Pure function;
    returns new memory, does NOT mutate input.
    """
    decayed = {
        agent: (1.0 - EMA_DECAY_ALPHA) * _resolve_weight(w)
               + EMA_DECAY_ALPHA * DEFAULT_AGENT_WEIGHT
        for agent, w in memory.get("agent_weights", {}).items()
    }
    return {
        **memory,
        "agent_weights": decayed,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


# ── SSE: structured event builder ─────────────────────────────────────


def build_memory_applied_event(
    memory: ProducerMemory,
    raw_pitches_by_agent: dict[str, list[Pitch]],
    adjusted_pitches_by_agent: dict[str, list[Pitch]],
) -> dict | None:
    """Build a producer.memory.applied event payload, or None when silent.

    Returns None when agent_weights is empty (bootstrap-fresh users → silent
    identity transform — no event on the SSE stream). Otherwise returns the
    event payload with per-agent pre/post max-priority for UI consumption.

    The raw `agent_weights` dict is included here for UI display ONLY. It
    is never routed into an LLM prompt — that's the whole point of applying
    memory deterministically upstream.
    """
    weights = memory.get("agent_weights", {})
    if not weights:
        return None
    changes = []
    for agent in weights:
        raw = raw_pitches_by_agent.get(agent) or []
        adj = adjusted_pitches_by_agent.get(agent) or []
        if not raw or not adj:
            continue
        changes.append(
            {
                "agent": agent,
                "pre_max_priority": max(p["priority"] for p in raw),
                "post_max_priority": max(p["priority"] for p in adj),
            }
        )
    return {"agent_weights": dict(weights), "changes": changes}


def emit_memory_applied(
    memory: ProducerMemory,
    raw_pitches_by_agent: dict[str, list[Pitch]],
    adjusted_pitches_by_agent: dict[str, list[Pitch]],
) -> None:
    """Build and emit `producer.memory.applied` if memory is non-empty.

    Per producer/docs/DESIGN.md §SSE: silent (no event) for bootstrap-fresh
    users — the identity transform produces no event on the trace.
    """
    payload = build_memory_applied_event(
        memory, raw_pitches_by_agent, adjusted_pitches_by_agent
    )
    if payload is not None:
        emit("producer.memory.applied", payload)

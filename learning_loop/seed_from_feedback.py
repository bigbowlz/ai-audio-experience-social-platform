"""Hydrate ProducerMemory from the v0 feedback log.

Reads the JSONL log written by player.cli_player (via storage.signals.append_signal),
computes a product of SIGNAL_MULTIPLIERS per agent, clamps to
[AGENT_WEIGHT_MIN, AGENT_WEIGHT_MAX], and seeds ProducerMemory via the
sanctioned demo seam.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Task 2.5
Addendum: imports iter_signals from storage.signals (not
learning_loop.feedback_log — that module was never created; addendum
relocated the signal log to the storage package).
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from learning_loop import seed_producer_memory
from producer.memory import (
    AGENT_WEIGHT_MAX,
    AGENT_WEIGHT_MIN,
    SIGNAL_MULTIPLIERS,
)
from storage.signals import iter_signals


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

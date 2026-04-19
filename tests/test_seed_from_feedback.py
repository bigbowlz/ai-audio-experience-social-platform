"""hydrate_producer_memory tests — weight computation + clamping.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Task 2.5 (addendum: imports
from storage.signals, not learning_loop.feedback_log).
"""
from __future__ import annotations

import math
from unittest import mock

from learning_loop.seed_from_feedback import (
    compute_weights,
    hydrate_producer_memory,
)


def test_compute_weights_product_of_multipliers():
    records = [
        {"agent": "weather", "signal": "like"},    # ×1.10
        {"agent": "weather", "signal": "like"},    # ×1.10
        {"agent": "calendar", "signal": "skip"},   # ×0.90
        {"agent": "youtube", "signal": "replay"},  # ×1.20
    ]
    weights = compute_weights(records)
    assert math.isclose(weights["weather"], 1.10 * 1.10, rel_tol=1e-9)
    assert math.isclose(weights["calendar"], 0.90, rel_tol=1e-9)
    assert math.isclose(weights["youtube"], 1.20, rel_tol=1e-9)


def test_compute_weights_clamps_at_max():
    records = [{"agent": "weather", "signal": "like"} for _ in range(10)]
    weights = compute_weights(records)
    assert weights["weather"] == 2.0


def test_compute_weights_clamps_at_min():
    records = [{"agent": "calendar", "signal": "skip"} for _ in range(15)]
    weights = compute_weights(records)
    assert weights["calendar"] == 0.3


def test_compute_weights_ignores_unknown_signal():
    records = [
        {"agent": "weather", "signal": "like"},
        {"agent": "weather", "signal": "pause"},   # not a learning signal
        {"agent": "weather", "signal": "unknown"},
    ]
    weights = compute_weights(records)
    assert math.isclose(weights["weather"], 1.10, rel_tol=1e-9)


def test_compute_weights_empty_returns_empty_dict():
    assert compute_weights([]) == {}


def test_hydrate_calls_seed_producer_memory(monkeypatch):
    # iter_signals is the addendum replacement for learning_loop.feedback_log.iter_signals.
    monkeypatch.setattr(
        "learning_loop.seed_from_feedback.iter_signals",
        lambda user_id: iter([
            {"agent": "weather", "signal": "like"},
            {"agent": "calendar", "signal": "skip"},
        ]),
    )
    seed = mock.Mock()
    monkeypatch.setattr(
        "learning_loop.seed_from_feedback.seed_producer_memory", seed
    )
    weights = hydrate_producer_memory("dev")
    seed.assert_called_once()
    args, _ = seed.call_args
    assert args[0] == "dev"
    assert "weather" in args[1] and "calendar" in args[1]
    assert weights == args[1]

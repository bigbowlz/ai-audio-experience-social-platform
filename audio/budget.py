"""Persistent budget tracking for ElevenLabs TTS usage.

Stores cumulative billed characters in a JSON file on disk.
Warns when approaching the configured budget ceiling.

Spec: audio/docs/DESIGN.md §Cost tracking
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from audio.config import BUDGET_CHAR_LIMIT, BUDGET_FILE, BUDGET_WARN_THRESHOLD

logger = logging.getLogger(__name__)

_COST_PER_CHAR = 0.30 / 1000  # $0.30 per 1K chars (Turbo v2.5 pay-as-you-go)


class BudgetState(TypedDict):
    billed_characters: int
    total_cost_usd: float
    last_updated: str


def _default_state() -> BudgetState:
    return BudgetState(
        billed_characters=0,
        total_cost_usd=0.0,
        last_updated=datetime.now(timezone.utc).isoformat(),
    )


def load(path: str = BUDGET_FILE) -> BudgetState:
    """Load budget state from disk. Returns default state if file missing."""
    p = Path(path)
    if not p.exists():
        return _default_state()
    try:
        with open(p) as f:
            data = json.load(f)
        return BudgetState(
            billed_characters=data.get("billed_characters", 0),
            total_cost_usd=data.get("total_cost_usd", 0.0),
            last_updated=data.get("last_updated", ""),
        )
    except (json.JSONDecodeError, OSError):
        logger.warning("Corrupt budget file %s, resetting", path)
        return _default_state()


def _save(state: BudgetState, path: str = BUDGET_FILE) -> None:
    """Write budget state atomically (tmp + rename)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
        tmp.rename(p)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def record(billed_characters: int, path: str = BUDGET_FILE) -> BudgetState:
    """Add billed characters from an episode and persist to disk.

    Logs a warning if cumulative usage crosses the budget warning threshold.

    Args:
        billed_characters: chars billed in this episode (including retries).
        path: budget file path (default from config).

    Returns:
        Updated BudgetState.
    """
    state = load(path)
    prev = state["billed_characters"]
    state["billed_characters"] = prev + billed_characters
    state["total_cost_usd"] = round(state["billed_characters"] * _COST_PER_CHAR, 2)
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    _save(state, path)

    warn_at = int(BUDGET_CHAR_LIMIT * BUDGET_WARN_THRESHOLD)
    if prev < BUDGET_CHAR_LIMIT <= state["billed_characters"]:
        logger.warning(
            "Budget EXCEEDED: %d / %d chars (%.0f%%). "
            "Estimated cost: $%.2f / $20.00. "
            "Generation continues — fund more if needed.",
            state["billed_characters"],
            BUDGET_CHAR_LIMIT,
            state["billed_characters"] / BUDGET_CHAR_LIMIT * 100,
            state["total_cost_usd"],
        )
    elif prev < warn_at <= state["billed_characters"]:
        logger.warning(
            "Budget warning: %d / %d chars used (%.0f%%). "
            "Estimated cost: $%.2f / $20.00",
            state["billed_characters"],
            BUDGET_CHAR_LIMIT,
            state["billed_characters"] / BUDGET_CHAR_LIMIT * 100,
            state["total_cost_usd"],
        )

    return state

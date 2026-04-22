"""Hydrate AgentMemory.topic_multiplier from a repo-local TOML config.

Read <repo>/config/topic_weights.toml (overridable path), expand
[categories] into per-topic weights for each [weights.<agent>] block,
apply per-topic overrides, clamp to [TOPIC_MULTIPLIER_MIN,
TOPIC_MULTIPLIER_MAX], and seed each (user_id, agent_name) record via
the learning-loop seed seam.

The config is committed to git so tuning is visible in history.

Precedence: per-topic entry > category entry > 1.0 default (via .get).

File schema:

    [categories]
    music = ["rock-music", "pop-music", "jazz", ...]

    [weights.youtube]
    music = 0.5                    # category: applies to every topic listed above
    "action-adventure-game" = 0.7  # per-topic override (wins over category)

    [weights.external]
    music = 0.3

Spec: agents/youtube/docs/DESIGN.md §AgentMemory schema (clamp range)
      learning_loop/docs/DESIGN.md §v0 stub contract (seed seam)
"""
from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any

from learning_loop import seed_topic_multiplier

log = logging.getLogger(__name__)

TOPIC_MULTIPLIER_MIN = 0.1
TOPIC_MULTIPLIER_MAX = 10.0

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "topic_weights.toml"


def _clamp(w: float) -> float:
    return max(TOPIC_MULTIPLIER_MIN, min(TOPIC_MULTIPLIER_MAX, w))


def load_config(path: Path) -> dict[str, Any]:
    """Parse the TOML config. Missing file → empty dict. Malformed → raises."""
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def resolve_weights(config: dict[str, Any], agent_name: str) -> dict[str, float]:
    """Expand categories, apply per-topic overrides, clamp.

    Returns {topic: weight} for the given agent. Empty dict if the agent
    has no [weights.<agent>] block.
    """
    categories: dict[str, list[str]] = config.get("categories", {})
    agent_block: dict[str, Any] = config.get("weights", {}).get(agent_name, {})
    if not agent_block:
        return {}

    resolved: dict[str, float] = {}

    # Pass 1: expand category entries.
    for key, value in agent_block.items():
        if key in categories:
            for topic in categories[key]:
                resolved[topic] = float(value)

    # Pass 2: per-topic overrides win (category keys already consumed by pass 1
    # are skipped here; any non-category key is treated as a specific topic).
    for key, value in agent_block.items():
        if key in categories:
            continue
        resolved[key] = float(value)

    return {t: _clamp(w) for t, w in resolved.items()}


def hydrate_topic_multipliers(
    user_id: str,
    path: Path | None = None,
) -> dict[str, dict[str, float]]:
    """Read config, resolve per-agent weights, seed each (user_id, agent).

    Returns {agent_name: {topic: weight}} so the caller can print a summary.
    Missing file or empty [weights] → returns {} and seeds nothing.
    """
    path = path or DEFAULT_CONFIG_PATH
    config = load_config(path)

    weights_table: dict[str, Any] = config.get("weights", {})
    if not isinstance(weights_table, dict):
        return {}

    # Warn once on unknown category references — the dev likely typo'd.
    categories = config.get("categories", {})
    for agent_name, agent_block in weights_table.items():
        if not isinstance(agent_block, dict):
            continue
        for key in agent_block:
            # Keys that aren't in categories are treated as specific topics;
            # we can't distinguish "unknown category" from "specific topic"
            # semantically, so only warn if the key looks category-like
            # (no hyphen → almost certainly a category name, not a slug).
            if key not in categories and "-" not in key and key != "music":
                log.warning(
                    "topic_weights.toml: key %r in [weights.%s] looks like a "
                    "category name but is not defined in [categories]",
                    key, agent_name,
                )

    seeded: dict[str, dict[str, float]] = {}
    for agent_name in weights_table:
        resolved = resolve_weights(config, agent_name)
        if resolved:
            seed_topic_multiplier(user_id, agent_name, resolved)
            seeded[agent_name] = resolved

    return seeded

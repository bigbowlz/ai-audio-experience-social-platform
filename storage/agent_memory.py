"""Per-agent memory scaffold (JSON). v0: read helper only; writes unused.

Path convention:
    ./data/agent_memory/{user_id}/{agent_name}.json

Per learning_loop/docs/DESIGN.md §v0 stub contract, nothing in v0 writes
agent memory — the stub contract guarantees empty/bootstrap reads. This
module exists so the v1 migration (unstubbing learning-loop) doesn't
need to introduce new path conventions: the file layout is already locked.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Addendum Task 2S.3
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DATA_ROOT = Path("data")
_AGENT_MEM_DIR = _DATA_ROOT / "agent_memory"


def _agent_memory_path(user_id: str, agent_name: str) -> Path:
    return _AGENT_MEM_DIR / user_id / f"{agent_name}.json"


def load_agent_memory(user_id: str, agent_name: str) -> dict[str, Any]:
    """Return parsed memory dict, or {} if the file is missing or malformed."""
    path = _agent_memory_path(user_id, agent_name)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_agent_memory(
    user_id: str, agent_name: str, memory: dict[str, Any]
) -> None:
    """Overwrite the agent's memory file. Unused in v0 (learning-loop stub)."""
    path = _agent_memory_path(user_id, agent_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(memory, indent=2, ensure_ascii=False))

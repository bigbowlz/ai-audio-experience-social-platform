"""learning_loop — STUBBED in v0.

The full learning-loop component (signal ingestion, session-end writes,
`memory.update.*` SSE) is deferred. See `learning_loop/docs/DESIGN.md`
§v0 stub contract for what this module ships today.

Runtime surface (v0):

    session_end(user_id, episode_id) -> None
        No-op; logs one line. Callable when orchestrator lands a
        session-end hook.

    load_agent_memory(user_id, agent_name) -> AgentMemory
        Returns the seeded record if one was installed via
        seed_topic_multiplier(); else bootstrap_memory().

    load_producer_memory(user_id) -> ProducerMemory
        Returns the seeded record if one was installed via
        seed_producer_memory(); else producer.memory.load_producer_memory().

Test/demo seam:

    seed_producer_memory(user_id, agent_weights) -> None
    seed_topic_multiplier(user_id, agent_name, multipliers) -> None
    clear_seeds() -> None
        Install pre-computed memory so the next load_* call returns it.
        Enables Episode B's running-order reorder beat without wiring
        signal-driven learning. Removed when the stub is unstubbed.

Import direction: learning_loop depends on agents.protocol and
producer.memory. Nothing in those modules imports learning_loop.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Mapping

from agents.protocol import AgentMemory, bootstrap_memory
from producer.memory import (
    ProducerMemory,
    load_producer_memory as _producer_load,
)


__all__ = [
    "clear_seeds",
    "compute_weights",
    "hydrate_producer_memory",
    "load_agent_memory",
    "load_producer_memory",
    "seed_producer_memory",
    "seed_topic_multiplier",
    "session_end",
]


logger = logging.getLogger(__name__)


_seeded_agent_memory: dict[tuple[str, str], AgentMemory] = {}
_seeded_producer_memory: dict[str, ProducerMemory] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def session_end(user_id: str, episode_id: str) -> None:
    """Session-end hook — STUBBED in v0.

    Emits one log line; does not write memory, does not emit
    memory.update.* SSE, does not ingest signals.
    """
    logger.info(
        "learning-loop: session_end stubbed — no memory writes in v0 "
        "(user_id=%s episode_id=%s)",
        user_id,
        episode_id,
    )


def load_agent_memory(user_id: str, agent_name: str) -> AgentMemory:
    """Return a seeded AgentMemory if one is installed; else bootstrap.

    v0 never persists — always in-memory. Delegates to bootstrap_memory()
    for non-seeded records.
    """
    seeded = _seeded_agent_memory.get((user_id, agent_name))
    if seeded is not None:
        return seeded
    return bootstrap_memory()


def load_producer_memory(user_id: str) -> ProducerMemory:
    """Return a seeded ProducerMemory if one is installed; else bootstrap.

    Delegates to producer.memory.load_producer_memory() for non-seeded
    records, which itself bootstraps in v0 (no persistence).
    """
    seeded = _seeded_producer_memory.get(user_id)
    if seeded is not None:
        return seeded
    return _producer_load(user_id)


def seed_producer_memory(
    user_id: str,
    agent_weights: Mapping[str, float],
) -> None:
    """Install a pre-computed ProducerMemory under user_id.

    The next call to load_producer_memory(user_id) returns the seeded
    record. Used by fixtures / Day-5 rehearsal scripts to show
    Episode B's running-order reorder beat without wiring signal-driven
    learning. Removed when the stub is unstubbed.
    """
    _seeded_producer_memory[user_id] = ProducerMemory(
        schema_version=1,
        agent_weights=dict(agent_weights),
        updated_at=_now_iso(),
    )


def seed_topic_multiplier(
    user_id: str,
    agent_name: str,
    multipliers: Mapping[str, float],
) -> None:
    """Install a pre-computed topic_multiplier on (user_id, agent_name).

    Leaves profile_state and other AgentMemory fields at their bootstrap
    defaults. Used by fixtures to show topic re-ranking in Episode B.
    Removed when the stub is unstubbed.
    """
    key = (user_id, agent_name)
    base = _seeded_agent_memory.get(key) or bootstrap_memory()
    _seeded_agent_memory[key] = AgentMemory(
        schema_version=base["schema_version"],
        profile_state=base["profile_state"],
        topic_multiplier=dict(multipliers),
        updated_at=_now_iso(),
    )


def clear_seeds() -> None:
    """Clear all in-memory seed state.

    Test-only. Call between tests that seed different memory states
    to avoid bleed.
    """
    _seeded_agent_memory.clear()
    _seeded_producer_memory.clear()


from learning_loop.seed_from_feedback import (  # noqa: E402, F401
    compute_weights,
    hydrate_producer_memory,
)

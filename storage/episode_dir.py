"""Episode directory conventions for v0 CLI storage.

Directory layout:
    ./data/episodes/{episode_id}/    — TTS segment output

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Addendum Task 2S.1
"""
from __future__ import annotations

import uuid
from pathlib import Path

_DATA_ROOT = Path("data")
_EPISODES_DIR = _DATA_ROOT / "episodes"


def new_episode_id() -> str:
    """Return a fresh uuid4 string. One call per CLI run."""
    return str(uuid.uuid4())


def episode_dir(episode_id: str) -> Path:
    """Return (and create on demand) ./data/episodes/{episode_id}/.

    Matches TTSClient's existing default output path layout
    (audio/tts.py:60 → `{output_dir}/{episode_id}/segment_{n}.mp3`).

    Caller must ensure `episode_id` is a bare uuid4 string (no path
    separators, no `..` segments). This function does not validate the
    input — path-traversal safety is an implicit contract maintained by
    the sole caller, `new_episode_id()`.
    """
    d = _EPISODES_DIR / episode_id
    d.mkdir(parents=True, exist_ok=True)
    return d

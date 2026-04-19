"""Per-episode feedback signal log (JSONL).

Path convention: ./data/signals/{user_id}/{episode_id}.jsonl
One file per episode; writer appends per-signal; reader globs all files
under the user dir (sorted by filename) for cross-episode hydration.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Addendum Task 2S.2
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict

_DATA_ROOT = Path("data")
_SIGNALS_DIR = _DATA_ROOT / "signals"


class FeedbackRecord(TypedDict):
    user_id: str
    episode_id: str
    segment_index: int
    agent: str
    pitch_title: str
    signal: str
    ts: str


def _signals_path(user_id: str, episode_id: str) -> Path:
    return _SIGNALS_DIR / user_id / f"{episode_id}.jsonl"


def append_signal(user_id: str, episode_id: str, record: FeedbackRecord) -> None:
    """Append one record to ./data/signals/{user_id}/{episode_id}.jsonl."""
    path = _signals_path(user_id, episode_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False))
        f.write("\n")


def iter_signals(user_id: str) -> Iterator[FeedbackRecord]:
    """Yield all records for a user across all episodes.

    Globs ./data/signals/{user_id}/*.jsonl (sorted by filename —
    lexicographic on uuid4 strings gives stable-but-arbitrary order;
    adequate for v0 since hydration is order-independent). Malformed
    lines are silently skipped.
    """
    user_dir = _SIGNALS_DIR / user_id
    if not user_dir.exists():
        return
    for path in sorted(user_dir.glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                yield rec

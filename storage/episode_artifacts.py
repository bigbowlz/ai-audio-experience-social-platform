"""Per-episode artifact persistence for the v0 CLI.

Writes structured input/output records for every step of episode generation
alongside the mp3 files, so each run leaves a complete audit trail under
`data/episodes/{episode_id}/`. Mirrors the step view printed by the CLI.

Layout (alongside segment_*.mp3):
    brief.json                  — assembled Brief (today_context, user_profile)
    pitches.json                — raw {agent: [Pitch, …]} before memory
    pitches_post_memory.json    — same dict after ProducerMemory is applied
    guaranteed.json             — Phase-1 guaranteed slots + budget
    bonus.json                  — Step-1.5 bonus picks + reasoning
    running_order.json          — final ordered segments with guaranteed/bonus tags
    opener_input.json           — exact LLM payload for the opener
    opener_output.txt           — LLM opener text
    segment_{i}_input.json      — per-segment LLM payload
    segment_{i}_output.json     — resulting SegmentScript
    sign_off_input.json         — LLM sign-off payload
    sign_off_output.txt         — LLM sign-off text
    episode_script.json         — final assembled EpisodeScript

Each writer is idempotent and atomic (tmp file + os.replace). Corrupted or
missing artifacts never raise — the CLI logs the failure and continues.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from storage.episode_dir import episode_dir

log = logging.getLogger(__name__)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("artifact write failed for %s: %r — continuing", path, exc)
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("artifact write failed for %s: %r — continuing", path, exc)
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def save_brief(episode_id: str, brief: dict) -> None:
    _write_json(episode_dir(episode_id) / "brief.json", brief)


def save_pitches(
    episode_id: str, pitches_by_agent: dict[str, list[dict]], *, post_memory: bool = False
) -> None:
    name = "pitches_post_memory.json" if post_memory else "pitches.json"
    _write_json(episode_dir(episode_id) / name, pitches_by_agent)


def save_guaranteed(
    episode_id: str, guaranteed: list[dict], bonus_budget_sec: int
) -> None:
    _write_json(
        episode_dir(episode_id) / "guaranteed.json",
        {"segments": guaranteed, "bonus_budget_sec": bonus_budget_sec},
    )


def save_bonus(
    episode_id: str,
    bonus: list[dict],
    guaranteed_reasons: list[dict],
    overall_reasoning: str,
) -> None:
    _write_json(
        episode_dir(episode_id) / "bonus.json",
        {
            "overall_reasoning": overall_reasoning,
            "guaranteed_reasons": guaranteed_reasons,
            "bonus_picks": bonus,
        },
    )


def save_running_order(
    episode_id: str, segments: list[dict], guaranteed_count: int
) -> None:
    """Save the final ordered running list with guaranteed/bonus tags."""
    tagged = [
        {**seg, "_slot_kind": "guaranteed" if i < guaranteed_count else "bonus"}
        for i, seg in enumerate(segments)
    ]
    _write_json(
        episode_dir(episode_id) / "running_order.json",
        {
            "segments": tagged,
            "guaranteed_count": guaranteed_count,
            "bonus_count": len(segments) - guaranteed_count,
            "total_sec": sum(s.get("suggested_length_sec", 0) for s in segments),
        },
    )


def save_opener(episode_id: str, payload: dict, output: str) -> None:
    d = episode_dir(episode_id)
    _write_json(d / "opener_input.json", payload)
    _write_text(d / "opener_output.txt", output)


def save_segment(
    episode_id: str, index: int, payload: dict, segment_script: dict
) -> None:
    d = episode_dir(episode_id)
    _write_json(d / f"segment_{index}_input.json", payload)
    _write_json(d / f"segment_{index}_output.json", segment_script)


def save_sign_off(episode_id: str, payload: dict, output: str) -> None:
    d = episode_dir(episode_id)
    _write_json(d / "sign_off_input.json", payload)
    _write_text(d / "sign_off_output.txt", output)


def save_episode_script(episode_id: str, episode_script: dict) -> None:
    _write_json(
        episode_dir(episode_id) / "episode_script.json", episode_script
    )

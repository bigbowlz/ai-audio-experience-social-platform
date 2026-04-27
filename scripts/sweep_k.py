"""Diagnostic: sweep BLEND_HALF_SATURATION_K and report music concentration
in each agent's pre-LLM top-8 candidate pool.

One-off. Not a test. Not a production change. See
docs/specs/2026-04-19-k-sweep-balance-check.md for design.

Run:
    python3 scripts/sweep_k.py
"""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from agents.youtube.extractor import extract_profile  # noqa: E402


# ── Config ───────────────────────────────────────────────────────────

K_VALUES = [0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500]
TOP_N = 8
MUSIC_CAP = 3

AGENTS: list[tuple[str, Path]] = [
    ("youtube", _REPO_ROOT / "ydata" / "user"),
    ("external", _REPO_ROOT / "agents" / "external" / "data"),
]

MUSIC_EXPLICIT = {"jazz", "rhythm-and-blues"}


def is_music(topic: str) -> bool:
    return (
        topic == "music"
        or topic.endswith("-music")
        or topic.startswith("music-of-")
        or topic in MUSIC_EXPLICIT
    )


# ── Loaders (mirrors agent loaders; kept inline so the script is standalone) ──


def _load_agent_data(
    data_dir: Path,
) -> tuple[list[dict], list[dict], dict[str, list[str]], dict[str, list[str]]]:
    with open(data_dir / "02_subscriptions.json") as f:
        subs = json.load(f)["items"]

    with open(data_dir / "03_likes.json") as f:
        likes = json.load(f)["items"]

    with open(data_dir / "07_topic_details.json") as f:
        chan_raw = json.load(f)
    channel_topics: dict[str, list[str]] = {}
    for item in chan_raw.get("items", []):
        cats = item.get("topicDetails", {}).get("topicCategories", [])
        if cats:
            channel_topics[item["id"]] = cats

    with open(data_dir / "08_video_topic_details.json") as f:
        vid_raw = json.load(f)
    video_topics: dict[str, list[str]] = {}
    for entry in vid_raw.get("per_video", []):
        tags = entry.get("tags", [])
        if tags:
            video_topics[entry["id"]] = tags

    return subs, likes, channel_topics, video_topics


# ── Re-blend + top-n (mirrors agent._top_n_seeded) ──────────────────


def _l1(scores: dict[str, float]) -> dict[str, float]:
    s = sum(scores.values())
    return {t: v / s for t, v in scores.items()} if s > 0 else {}


def _top_n_seeded(score: dict[str, float], n: int, seed: tuple) -> list[str]:
    rng = random.Random(str(seed))
    keyed = [(v, rng.random(), k) for k, v in score.items()]
    keyed.sort(reverse=True)
    return [k for _, _, k in keyed[: min(n, len(keyed))]]


def reblend_top_n(
    long_term: dict[str, float],
    recent: dict[str, float],
    total_recent_weight: float,
    k: float,
    seed: tuple,
    n: int,
) -> tuple[list[str], float]:
    alpha = (
        total_recent_weight / (total_recent_weight + k)
        if (total_recent_weight + k) > 0
        else 0.0
    )
    topics = set(long_term) | set(recent)
    combined = {
        t: (1 - alpha) * long_term.get(t, 0.0) + alpha * recent.get(t, 0.0)
        for t in topics
    }
    combined = _l1(combined)
    return _top_n_seeded(combined, n=n, seed=seed), alpha


# ── Driver ──────────────────────────────────────────────────────────


def _starred(topics: list[str]) -> str:
    return "[" + ", ".join(f"*{t}*" if is_music(t) else t for t in topics) + "]"


def main() -> None:
    now = datetime.now(timezone.utc)

    # Extract once per agent — K only affects re-blend, not the windows.
    extracted: dict[str, dict] = {}
    for name, data_dir in AGENTS:
        subs, likes, ch_topics, vid_topics = _load_agent_data(data_dir)
        profile = extract_profile(subs, likes, ch_topics, vid_topics, now)
        extracted[name] = {
            "long_term": profile["long_term_topic_scores"],
            "recent": profile["recent_topic_scores"],
            "total_recent_weight": profile["stats"]["total_recent_weight"],
            "computed_at": profile["computed_at"],
            "stats": profile["stats"],
        }

    print("## Pre-LLM top-8 music concentration sweep\n")
    for name, e in extracted.items():
        s = e["stats"]
        print(
            f"{name}: subs={s['total_subs']}  likes={s['total_likes']}  "
            f"unique_topics={s['unique_topics']}  "
            f"total_recent_weight={e['total_recent_weight']:.2f}"
        )
    print()

    # Per-K, per-agent candidate pools.
    music_counts: dict[float, dict[str, int]] = {}
    for k in K_VALUES:
        print(f"K={k}")
        music_counts[k] = {}
        for name, e in extracted.items():
            seed = ("sweep", e["computed_at"])
            candidates, alpha = reblend_top_n(
                e["long_term"],
                e["recent"],
                e["total_recent_weight"],
                k,
                seed,
                TOP_N,
            )
            m = sum(1 for t in candidates if is_music(t))
            music_counts[k][name] = m
            print(
                f"  {name:9s} α={alpha:.3f}  music/{TOP_N}={m}  {_starred(candidates)}"
            )
        print()

    # Summary.
    winners = [
        k
        for k in K_VALUES
        if all(music_counts[k][name] <= MUSIC_CAP for name, _ in AGENTS)
    ]
    print("---")
    if winners:
        k_min = min(winners)
        print(
            f"Smallest K with music/{TOP_N} ≤ {MUSIC_CAP} for BOTH agents: K={k_min}"
        )
        print(f"All K meeting the criterion in sweep: {winners}")
    else:
        print(
            f"No K in {K_VALUES} yields music/{TOP_N} ≤ {MUSIC_CAP} for both "
            f"agents — likes-concentration is structural at this scope."
        )


if __name__ == "__main__":
    main()

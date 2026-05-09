"""Diagnostic: sweep BLEND_HALF_SATURATION_K and report music concentration
in each agent's pre-LLM top-8 candidate pool.

One-off. Not a test. Not a production change. See
docs/specs/2026-04-19-k-sweep-balance-check.md for design.

Run:
    python3 scripts/sweep_k.py
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from agents.youtube.extractor import extract_profile, TopicMeta  # noqa: E402
from agents.youtube.agent import _load_probe_data  # noqa: E402
from agents.external.agent import _load_data  # noqa: E402


# ── Config ───────────────────────────────────────────────────────────

K_VALUES = [0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500]
TOP_N = 8
MUSIC_CAP = 3

AGENTS: list[tuple[str, Path, callable]] = [
    ("youtube", _REPO_ROOT / "ydata" / "user", _load_probe_data),
    ("external", _REPO_ROOT / "agents" / "external" / "data", _load_data),
]

# Explicit music-family labels that don't contain the substring "music"
MUSIC_EXPLICIT_LABELS = {
    "jazz",
    "rhythm and blues",
}


def is_music(qid: str, topic_meta: dict[str, TopicMeta]) -> bool:
    label = topic_meta.get(qid, {}).get("label", "").lower()
    if not label:
        return False
    return "music" in label or label in MUSIC_EXPLICIT_LABELS


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


def _starred(qids: list[str], topic_meta: dict[str, TopicMeta]) -> str:
    """Render a candidate list as labels, starring music topics."""
    parts = []
    for q in qids:
        label = topic_meta.get(q, {}).get("label", q)
        parts.append(f"*{label}*" if is_music(q, topic_meta) else label)
    return "[" + ", ".join(parts) + "]"


def main() -> None:
    now = datetime.now(timezone.utc)

    # Extract once per agent — K only affects re-blend, not the windows.
    extracted: dict[str, dict] = {}
    for name, data_dir, loader in AGENTS:
        subs, likes, channel_qids, video_qids, topic_meta = loader(data_dir)
        profile = extract_profile(
            subs, likes, channel_qids, video_qids, now, topic_meta=topic_meta,
        )
        extracted[name] = {
            "long_term": profile["long_term_topic_scores"],
            "recent": profile["recent_topic_scores"],
            "total_recent_weight": profile["stats"]["total_recent_weight"],
            "computed_at": profile["computed_at"],
            "stats": profile["stats"],
            "topic_meta": profile["topic_meta"],
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
        for name, _, _ in AGENTS:
            e = extracted[name]
            seed = ("sweep", e["computed_at"])
            candidates, alpha = reblend_top_n(
                e["long_term"],
                e["recent"],
                e["total_recent_weight"],
                k,
                seed,
                TOP_N,
            )
            m = sum(1 for q in candidates if is_music(q, e["topic_meta"]))
            music_counts[k][name] = m
            print(
                f"  {name:9s} α={alpha:.3f}  music/{TOP_N}={m}  "
                f"{_starred(candidates, e['topic_meta'])}"
            )
        print()

    # Summary.
    winners = [
        k
        for k in K_VALUES
        if all(music_counts[k][name] <= MUSIC_CAP for name, _, _ in AGENTS)
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

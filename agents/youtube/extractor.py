"""Pure extraction: YouTube API responses → InterestProfile.

No I/O, no OAuth, no API calls. Callers handle acquisition;
this module transforms structured data into a scored profile.

Spec: agents/youtube/docs/DESIGN.md
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import TypedDict, Literal
from urllib.parse import unquote


# ── Types ────────────────────────────────────────────────────────────

class Contributor(TypedDict):
    kind: Literal["sub", "like"]
    channel_name: str
    channel_id: str
    subscribed_at: str | None  # ISO; present iff kind == "sub"
    liked_at: str | None       # ISO; present iff kind == "like"
    video_title: str | None    # present iff kind == "like"
    video_id: str | None       # present iff kind == "like"


class InterestProfile(TypedDict):
    long_term_topic_scores: dict[str, float]
    recent_topic_scores: dict[str, float]
    combined_topic_scores: dict[str, float]
    topic_provenance: dict[str, list[Contributor]]
    computed_at: str
    stats: dict


# ── Constants ────────────────────────────────────────────────────────

RECENT_HALF_LIFE_DAYS = 90
BLEND_HALF_SATURATION_K = 100.0
PROVENANCE_K = 5
PROVENANCE_SUB_SLOTS = 2
PROVENANCE_LIKE_SLOTS = 3

_WIKI_URL_PREFIX = "https://en.wikipedia.org/wiki/"
_PAREN_SUFFIX = re.compile(r"_\([^)]*\)$")


# ── Helpers ──────────────────────────────────────────────────────────

def normalize_topic(wiki_url: str) -> str:
    """Wikipedia URL → kebab-case topic tag.

    Rule: last path segment, URL-decode, strip parenthetical suffixes,
    _ → -, lowercase.
    """
    segment = wiki_url.rsplit("/", 1)[-1]
    segment = unquote(segment)
    segment = _PAREN_SUFFIX.sub("", segment)
    return segment.replace("_", "-").lower()


def _decayed_weight(liked_at: datetime, now: datetime) -> float:
    age_days = max(0.0, (now - liked_at).total_seconds() / 86400.0)
    return math.exp(-age_days * math.log(2) / RECENT_HALF_LIFE_DAYS)


def _parse_iso(s: str) -> datetime:
    # Handle various ISO formats from YouTube API
    s = s.rstrip("Z") + "+00:00" if s.endswith("Z") else s
    return datetime.fromisoformat(s)


def _l1_normalize(scores: dict[str, float]) -> dict[str, float]:
    s = sum(scores.values())
    if s > 0:
        return {t: v / s for t, v in scores.items()}
    return {}


# ── Core: extract_profile ────────────────────────────────────────────

def extract_profile(
    subs: list[dict],
    likes: list[dict],
    channel_topics: dict[str, list[str]],
    video_topics: dict[str, list[str]],
    now: datetime,
) -> InterestProfile:
    """Build an InterestProfile from YouTube API responses.

    Args:
        subs: subscription items (snippet.resourceId.channelId, snippet.publishedAt, snippet.title)
        likes: liked-video playlist items (snippet.videoOwnerChannelId, snippet.publishedAt,
               snippet.videoOwnerChannelTitle, snippet.title, contentDetails.videoId / snippet.resourceId.videoId)
        channel_topics: channel_id → list of Wikipedia topic URLs
        video_topics: video_id → list of Wikipedia topic URLs or page names (e.g. "Rock_music")
        now: reference time for recency decay
    """
    now_iso = now.isoformat()

    # ── Parse subs ───────────────────────────────────────────────────
    parsed_subs: list[dict] = []
    for item in subs:
        snippet = item["snippet"]
        channel_id = snippet["resourceId"]["channelId"]
        parsed_subs.append({
            "channel_id": channel_id,
            "channel_name": snippet["title"],
            "subscribed_at": snippet["publishedAt"],
        })

    # ── Parse likes ──────────────────────────────────────────────────
    parsed_likes: list[dict] = []
    for item in likes:
        snippet = item["snippet"]
        # Deleted/private videos may lack videoOwnerChannelId — skip them
        if "videoOwnerChannelId" not in snippet:
            continue
        video_id = (
            item.get("contentDetails", {}).get("videoId")
            or snippet["resourceId"]["videoId"]
        )
        parsed_likes.append({
            "channel_id": snippet["videoOwnerChannelId"],
            "channel_name": snippet["videoOwnerChannelTitle"],
            "liked_at": snippet["publishedAt"],
            "video_title": snippet["title"],
            "video_id": video_id,
        })

    # ── Normalize channel topics (Wikipedia URLs → kebab) ────────────
    chan_topics_norm: dict[str, list[str]] = {}
    for cid, urls in channel_topics.items():
        chan_topics_norm[cid] = [normalize_topic(u) for u in urls]

    # Normalize video topics — same rule as channel topics; works on both
    # full Wikipedia URLs and bare page names (e.g. "Rock_music").
    vid_topics_norm: dict[str, list[str]] = {
        vid: [normalize_topic(t) for t in tags]
        for vid, tags in video_topics.items()
    }

    # ── Long-term TF (from subs, flat weight) ────────────────────────
    tf_long: dict[str, float] = {}
    for sub in parsed_subs:
        topics = chan_topics_norm.get(sub["channel_id"], [])
        for t in topics:
            tf_long[t] = tf_long.get(t, 0.0) + 1.0

    # ── Recent TF (from likes, decayed weight) ───────────────────────
    tf_recent: dict[str, float] = {}
    total_recent_weight = 0.0
    for like in parsed_likes:
        liked_dt = _parse_iso(like["liked_at"])
        w = _decayed_weight(liked_dt, now)
        total_recent_weight += w
        topics = vid_topics_norm.get(like["video_id"], [])
        for t in topics:
            tf_recent[t] = tf_recent.get(t, 0.0) + w

    # ── Sublinear TF scaling ─────────────────────────────────────────
    tf_long = {t: math.log(1 + v) for t, v in tf_long.items()}
    tf_recent = {t: math.log(1 + v) for t, v in tf_recent.items()}

    # ── Per-window IDF ───────────────────────────────────────────────
    n_long = len(parsed_subs)
    n_recent = len(parsed_likes)

    # Long-term: df = count of subs whose topic list contains T
    df_long: dict[str, int] = {}
    for sub in parsed_subs:
        topics = chan_topics_norm.get(sub["channel_id"], [])
        for t in set(topics):
            df_long[t] = df_long.get(t, 0) + 1

    # Recent: df = count of likes whose video topic list contains T
    df_recent: dict[str, int] = {}
    for like in parsed_likes:
        topics = vid_topics_norm.get(like["video_id"], [])
        for t in set(topics):
            df_recent[t] = df_recent.get(t, 0) + 1

    # ── Score = TF × IDF per window, then L1-normalize ───────────────
    long_term_scores: dict[str, float] = {}
    if n_long > 0:
        for t, tf_val in tf_long.items():
            idf = math.log((n_long + 1) / df_long[t])
            long_term_scores[t] = tf_val * idf
        long_term_scores = _l1_normalize(long_term_scores)

    recent_scores: dict[str, float] = {}
    if n_recent > 0:
        for t, tf_val in tf_recent.items():
            idf = math.log((n_recent + 1) / df_recent[t])
            recent_scores[t] = tf_val * idf
        recent_scores = _l1_normalize(recent_scores)

    # ── Blend: combined_topic_scores ─────────────────────────────────
    alpha = total_recent_weight / (total_recent_weight + BLEND_HALF_SATURATION_K) if (total_recent_weight + BLEND_HALF_SATURATION_K) > 0 else 0.0
    all_topics = set(long_term_scores.keys()) | set(recent_scores.keys())
    combined: dict[str, float] = {}
    for t in all_topics:
        combined[t] = (1 - alpha) * long_term_scores.get(t, 0.0) + alpha * recent_scores.get(t, 0.0)
    combined = _l1_normalize(combined)

    # ── Provenance: K=5 per topic ────────────────────────────────────
    # Build per-topic contributor pools
    topic_sub_contributors: dict[str, list[Contributor]] = {}
    for sub in parsed_subs:
        topics = chan_topics_norm.get(sub["channel_id"], [])
        for t in topics:
            c: Contributor = {
                "kind": "sub",
                "channel_name": sub["channel_name"],
                "channel_id": sub["channel_id"],
                "subscribed_at": sub["subscribed_at"],
                "liked_at": None,
                "video_title": None,
                "video_id": None,
            }
            topic_sub_contributors.setdefault(t, []).append(c)

    topic_like_contributors: dict[str, list[Contributor]] = {}
    for like in parsed_likes:
        topics = vid_topics_norm.get(like["video_id"], [])
        for t in topics:
            c: Contributor = {
                "kind": "like",
                "channel_name": like["channel_name"],
                "channel_id": like["channel_id"],
                "subscribed_at": None,
                "liked_at": like["liked_at"],
                "video_title": like["video_title"],
                "video_id": like["video_id"],
            }
            topic_like_contributors.setdefault(t, []).append(c)

    topic_provenance: dict[str, list[Contributor]] = {}
    for t in all_topics:
        # Subs: sort by subscribed_at ascending (oldest first)
        sub_pool = sorted(
            topic_sub_contributors.get(t, []),
            key=lambda c: c["subscribed_at"] or "",
        )
        # Likes: sort by liked_at descending (most recent first)
        like_pool = sorted(
            topic_like_contributors.get(t, []),
            key=lambda c: c["liked_at"] or "",
            reverse=True,
        )

        # Take up to PROVENANCE_SUB_SLOTS subs, up to PROVENANCE_LIKE_SLOTS likes
        selected_subs = sub_pool[:PROVENANCE_SUB_SLOTS]
        selected_likes = like_pool[:PROVENANCE_LIKE_SLOTS]

        # Fill shortfall from the other side (continuing same sort order)
        sub_shortfall = PROVENANCE_SUB_SLOTS - len(selected_subs)
        like_shortfall = PROVENANCE_LIKE_SLOTS - len(selected_likes)

        if sub_shortfall > 0:
            # Fill from likes continuing in liked_at desc order
            extra_likes = like_pool[PROVENANCE_LIKE_SLOTS:PROVENANCE_LIKE_SLOTS + sub_shortfall]
            selected_likes.extend(extra_likes)
        elif like_shortfall > 0:
            # Fill from subs continuing in subscribed_at asc order
            extra_subs = sub_pool[PROVENANCE_SUB_SLOTS:PROVENANCE_SUB_SLOTS + like_shortfall]
            selected_subs.extend(extra_subs)

        # Concatenate: subs block then likes block, up to K total
        contributors = (selected_subs + selected_likes)[:PROVENANCE_K]
        if contributors:
            topic_provenance[t] = contributors

    # ── Stats ────────────────────────────────────────────────────────
    all_entities = len(parsed_subs) + len(parsed_likes)
    tagged_subs = sum(1 for s in parsed_subs if chan_topics_norm.get(s["channel_id"]))
    tagged_likes = sum(1 for l in parsed_likes if vid_topics_norm.get(l["video_id"]))
    tagged_entities = tagged_subs + tagged_likes
    tag_coverage = tagged_entities / all_entities if all_entities > 0 else 0.0

    topic_counts: list[int] = []
    for s in parsed_subs:
        topics = chan_topics_norm.get(s["channel_id"], [])
        if topics:
            topic_counts.append(len(topics))
    for l in parsed_likes:
        topics = vid_topics_norm.get(l["video_id"], [])
        if topics:
            topic_counts.append(len(topics))
    avg_topics = sum(topic_counts) / len(topic_counts) if topic_counts else 0.0

    stats = {
        "total_subs": len(parsed_subs),
        "total_likes": len(parsed_likes),
        "total_recent_weight": total_recent_weight,
        "unique_topics": len(all_topics),
        "tag_coverage_pct": round(tag_coverage * 100, 1),
        "avg_topics_per_entity": round(avg_topics, 2),
    }

    return InterestProfile(
        long_term_topic_scores=long_term_scores,
        recent_topic_scores=recent_scores,
        combined_topic_scores=combined,
        topic_provenance=topic_provenance,
        computed_at=now_iso,
        stats=stats,
    )

"""Pure extraction: YouTube API responses → InterestProfile.

No I/O, no OAuth, no API calls. Callers handle acquisition and
canonicalization (see agents/youtube/canonicalize.py); this module
transforms structured data into a scored profile keyed by Wikidata QID.

Scoring: fractional counting (1/|T_d| per item). Each sub or like casts
a unit vote split uniformly across its topics; over-tagged items dilute
their own vote. Recent likes are recency-decayed (90-day half-life).
The two streams are L1-normalized and blended via a sample-size-aware α.

Spec: agents/youtube/docs/DESIGN.md
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import TypedDict, Literal

# ── Types ────────────────────────────────────────────────────────────


class Contributor(TypedDict):
    kind: Literal["sub", "like"]
    channel_name: str
    channel_id: str
    subscribed_at: str | None  # ISO; present iff kind == "sub"
    liked_at: str | None  # ISO; present iff kind == "like"
    video_title: str | None  # present iff kind == "like"
    video_id: str | None  # present iff kind == "like"


class TopicMeta(TypedDict):
    label: str  # human-readable, e.g. "Rock music"
    canonical_url: str  # post-redirect Wikipedia URL


class InterestProfile(TypedDict):
    long_term_topic_scores: dict[str, float]  # QID → weight
    recent_topic_scores: dict[str, float]  # QID → weight
    combined_topic_scores: dict[str, float]  # QID → weight
    topic_provenance: dict[str, list[Contributor]]  # QID → contributors
    topic_meta: dict[str, TopicMeta]  # QID → label + canonical URL
    computed_at: str
    stats: dict


# ── Constants ────────────────────────────────────────────────────────

RECENT_HALF_LIFE_DAYS = 90
BLEND_HALF_SATURATION_K = 10
PROVENANCE_K = 5
PROVENANCE_SUB_SLOTS = 2
PROVENANCE_LIKE_SLOTS = 3


# ── Helpers ──────────────────────────────────────────────────────────


def primary_anchor(provenance: list[Contributor]) -> str | None:
    """Return the video_id of a topic's most-prominent liked video, if any.

    Used to deduplicate pitches that would otherwise narrate the same
    source video under two different topic labels. Topics with no
    like-evidence return None and are not subject to anchor dedup.
    Provenance is sorted likes-newest-first inside extract_profile, so
    the first like is the freshest evidence and the most likely
    narrative anchor.
    """
    for c in provenance:
        if c["kind"] == "like" and c.get("video_id"):
            return c["video_id"]
    return None


def dedupe_bundle_by_anchor(bundle: list[dict]) -> list[dict]:
    """Drop bundle items whose primary anchor was already seen earlier.

    Walks the bundle in the order it was assembled (already score-sorted)
    and keeps the first occurrence of each anchor. Items with no anchor
    (sub-only topics) always pass through.
    """
    seen: set[str] = set()
    kept: list[dict] = []
    for item in bundle:
        anchor = primary_anchor(item.get("provenance", []))
        if anchor and anchor in seen:
            continue
        if anchor:
            seen.add(anchor)
        kept.append(item)
    return kept


def _decayed_weight(liked_at: datetime, now: datetime) -> float:
    age_days = max(0.0, (now - liked_at).total_seconds() / 86400.0)
    return math.exp(-age_days * math.log(2) / RECENT_HALF_LIFE_DAYS)


def _parse_iso(s: str) -> datetime:
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
    channel_qids: dict[str, list[str]],
    video_qids: dict[str, list[str]],
    now: datetime,
    topic_meta: dict[str, TopicMeta] | None = None,
) -> InterestProfile:
    """Build an InterestProfile from YouTube API responses.

    Args:
        subs: subscription items (snippet.resourceId.channelId, snippet.publishedAt, snippet.title)
        likes: liked-video playlist items (snippet.videoOwnerChannelId, snippet.publishedAt,
               snippet.videoOwnerChannelTitle, snippet.title, contentDetails.videoId / snippet.resourceId.videoId)
        channel_qids: channel_id → list of Wikidata QIDs (canonicalized upstream)
        video_qids: video_id → list of Wikidata QIDs (canonicalized upstream)
        now: reference time for recency decay
        topic_meta: optional QID → {label, canonical_url} for downstream display
    """
    now_iso = now.isoformat()
    topic_meta = topic_meta or {}

    # ── Parse subs ───────────────────────────────────────────────────
    parsed_subs: list[dict] = []
    for item in subs:
        snippet = item["snippet"]
        channel_id = snippet["resourceId"]["channelId"]
        parsed_subs.append(
            {
                "channel_id": channel_id,
                "channel_name": snippet["title"],
                "subscribed_at": snippet["publishedAt"],
            }
        )

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
        parsed_likes.append(
            {
                "channel_id": snippet["videoOwnerChannelId"],
                "channel_name": snippet["videoOwnerChannelTitle"],
                "liked_at": snippet["publishedAt"],
                "video_title": snippet["title"],
                "video_id": video_id,
            }
        )

    # ── Long-term: each sub contributes 1/|T| to each of its QIDs ────
    w_long: dict[str, float] = {}
    for sub in parsed_subs:
        qids = channel_qids.get(sub["channel_id"], [])
        if not qids:
            continue
        share = 1.0 / len(qids)
        for q in qids:
            w_long[q] = w_long.get(q, 0.0) + share

    # ── Recent: each like contributes δ(d)/|T| (decayed by recency) ──
    w_recent: dict[str, float] = {}
    total_recent_weight = 0.0
    for like in parsed_likes:
        liked_dt = _parse_iso(like["liked_at"])
        decay = _decayed_weight(liked_dt, now)
        total_recent_weight += decay
        qids = video_qids.get(like["video_id"], [])
        if not qids:
            continue
        share = decay / len(qids)
        for q in qids:
            w_recent[q] = w_recent.get(q, 0.0) + share

    long_term_scores = _l1_normalize(w_long)
    recent_scores = _l1_normalize(w_recent)

    # ── Blend ────────────────────────────────────────────────────────
    denom = total_recent_weight + BLEND_HALF_SATURATION_K
    alpha = total_recent_weight / denom if denom > 0 else 0.0
    all_topics = set(long_term_scores.keys()) | set(recent_scores.keys())
    combined: dict[str, float] = {}
    for t in all_topics:
        combined[t] = (1 - alpha) * long_term_scores.get(
            t, 0.0
        ) + alpha * recent_scores.get(t, 0.0)
    combined = _l1_normalize(combined)

    # ── Provenance: K=5 per topic ────────────────────────────────────
    topic_sub_contributors: dict[str, list[Contributor]] = {}
    for sub in parsed_subs:
        qids = channel_qids.get(sub["channel_id"], [])
        for q in qids:
            c: Contributor = {
                "kind": "sub",
                "channel_name": sub["channel_name"],
                "channel_id": sub["channel_id"],
                "subscribed_at": sub["subscribed_at"],
                "liked_at": None,
                "video_title": None,
                "video_id": None,
            }
            topic_sub_contributors.setdefault(q, []).append(c)

    topic_like_contributors: dict[str, list[Contributor]] = {}
    for like in parsed_likes:
        qids = video_qids.get(like["video_id"], [])
        for q in qids:
            c: Contributor = {
                "kind": "like",
                "channel_name": like["channel_name"],
                "channel_id": like["channel_id"],
                "subscribed_at": None,
                "liked_at": like["liked_at"],
                "video_title": like["video_title"],
                "video_id": like["video_id"],
            }
            topic_like_contributors.setdefault(q, []).append(c)

    topic_provenance: dict[str, list[Contributor]] = {}
    for t in all_topics:
        sub_pool = sorted(
            topic_sub_contributors.get(t, []),
            key=lambda c: c["subscribed_at"] or "",
        )
        like_pool = sorted(
            topic_like_contributors.get(t, []),
            key=lambda c: c["liked_at"] or "",
            reverse=True,
        )

        selected_subs = sub_pool[:PROVENANCE_SUB_SLOTS]
        selected_likes = like_pool[:PROVENANCE_LIKE_SLOTS]

        sub_shortfall = PROVENANCE_SUB_SLOTS - len(selected_subs)
        like_shortfall = PROVENANCE_LIKE_SLOTS - len(selected_likes)

        if sub_shortfall > 0:
            extra_likes = like_pool[
                PROVENANCE_LIKE_SLOTS : PROVENANCE_LIKE_SLOTS + sub_shortfall
            ]
            selected_likes.extend(extra_likes)
        elif like_shortfall > 0:
            extra_subs = sub_pool[
                PROVENANCE_SUB_SLOTS : PROVENANCE_SUB_SLOTS + like_shortfall
            ]
            selected_subs.extend(extra_subs)

        contributors = (selected_subs + selected_likes)[:PROVENANCE_K]
        if contributors:
            topic_provenance[t] = contributors

    # ── topic_meta: prune to topics actually in this profile ─────────
    profile_meta: dict[str, TopicMeta] = {
        q: topic_meta[q] for q in all_topics if q in topic_meta
    }

    # ── Stats ────────────────────────────────────────────────────────
    all_entities = len(parsed_subs) + len(parsed_likes)
    tagged_subs = sum(1 for s in parsed_subs if channel_qids.get(s["channel_id"]))
    tagged_likes = sum(1 for l in parsed_likes if video_qids.get(l["video_id"]))
    tagged_entities = tagged_subs + tagged_likes
    tag_coverage = tagged_entities / all_entities if all_entities > 0 else 0.0

    topic_counts: list[int] = []
    for s in parsed_subs:
        qids = channel_qids.get(s["channel_id"], [])
        if qids:
            topic_counts.append(len(qids))
    for l in parsed_likes:
        qids = video_qids.get(l["video_id"], [])
        if qids:
            topic_counts.append(len(qids))
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
        topic_meta=profile_meta,
        computed_at=now_iso,
        stats=stats,
    )

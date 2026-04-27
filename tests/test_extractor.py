"""Tests for agents/youtube/extractor.py.

Uses committed probe JSON at ydata/user/ as fixtures.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agents.youtube.extractor import (
    InterestProfile,
    extract_profile,
    normalize_topic,
    _decayed_weight,
    RECENT_HALF_LIFE_DAYS,
)

PROBE_DIR = Path(__file__).resolve().parent.parent / "ydata" / "user"

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def probe_subs():
    with open(PROBE_DIR / "02_subscriptions.json") as f:
        return json.load(f)["items"]


@pytest.fixture
def probe_likes():
    with open(PROBE_DIR / "03_likes.json") as f:
        return json.load(f)["items"]


@pytest.fixture
def probe_channel_topics():
    """channel_id → list of Wikipedia topic URLs."""
    with open(PROBE_DIR / "07_topic_details.json") as f:
        data = json.load(f)
    return {
        item["id"]: item["topicDetails"]["topicCategories"]
        for item in data["items"]
        if "topicDetails" in item
    }


@pytest.fixture
def probe_video_topics():
    """video_id → list of raw Wikipedia page names (e.g. 'Rock_music').
    extract_profile normalizes these via normalize_topic(), same as channel topics.
    """
    with open(PROBE_DIR / "08_video_topic_details.json") as f:
        data = json.load(f)
    return {entry["id"]: entry["tags"] for entry in data["per_video"]}


@pytest.fixture
def now():
    return datetime(2026, 4, 16, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def real_profile(probe_subs, probe_likes, probe_channel_topics, probe_video_topics, now):
    return extract_profile(probe_subs, probe_likes, probe_channel_topics, probe_video_topics, now)


# ── normalize_topic ──────────────────────────────────────────────────


class TestNormalizeTopic:
    def test_simple(self):
        assert normalize_topic("https://en.wikipedia.org/wiki/Rock_music") == "rock-music"

    def test_strip_parenthetical(self):
        assert normalize_topic("https://en.wikipedia.org/wiki/Lifestyle_(sociology)") == "lifestyle"

    def test_multi_word(self):
        assert normalize_topic("https://en.wikipedia.org/wiki/Video_game_culture") == "video-game-culture"

    def test_single_word(self):
        assert normalize_topic("https://en.wikipedia.org/wiki/Music") == "music"

    def test_url_encoded(self):
        assert normalize_topic("https://en.wikipedia.org/wiki/Caf%C3%A9") == "café"


# ── Recency decay ───────────────────────────────────────────────────


class TestDecayedWeight:
    def test_zero_age(self):
        now = datetime(2026, 4, 16, tzinfo=timezone.utc)
        assert _decayed_weight(now, now) == pytest.approx(1.0)

    def test_half_life(self):
        now = datetime(2026, 4, 16, tzinfo=timezone.utc)
        past = datetime(2026, 1, 16, tzinfo=timezone.utc)  # exactly 90 days ago
        assert _decayed_weight(past, now) == pytest.approx(0.5, abs=0.01)

    def test_future_clamped(self):
        now = datetime(2026, 4, 16, tzinfo=timezone.utc)
        future = datetime(2026, 4, 17, tzinfo=timezone.utc)
        # liked_at > now should clamp to weight = 1.0
        assert _decayed_weight(future, now) == pytest.approx(1.0)

    def test_old_signal_decays(self):
        now = datetime(2026, 4, 16, tzinfo=timezone.utc)
        old = datetime(2018, 1, 1, tzinfo=timezone.utc)
        w = _decayed_weight(old, now)
        assert w < 0.001


# ── extract_profile on real probe data ───────────────────────────────


class TestExtractProfileReal:
    def test_stats_counts(self, real_profile):
        assert real_profile["stats"]["total_subs"] == 96
        # 5 of 77 liked videos lack videoOwnerChannelId (deleted/private) and are skipped
        assert real_profile["stats"]["total_likes"] == 72

    def test_long_term_l1(self, real_profile):
        scores = real_profile["long_term_topic_scores"]
        if scores:
            assert sum(scores.values()) == pytest.approx(1.0, abs=1e-9)

    def test_recent_l1(self, real_profile):
        scores = real_profile["recent_topic_scores"]
        if scores:
            assert sum(scores.values()) == pytest.approx(1.0, abs=1e-9)

    def test_combined_l1(self, real_profile):
        scores = real_profile["combined_topic_scores"]
        if scores:
            assert sum(scores.values()) == pytest.approx(1.0, abs=1e-9)

    def test_combined_is_superset(self, real_profile):
        lt_keys = set(real_profile["long_term_topic_scores"].keys())
        rt_keys = set(real_profile["recent_topic_scores"].keys())
        cb_keys = set(real_profile["combined_topic_scores"].keys())
        assert cb_keys == lt_keys | rt_keys

    def test_provenance_per_topic_capped(self, real_profile):
        for t, contribs in real_profile["topic_provenance"].items():
            assert len(contribs) <= 5, f"topic {t} has {len(contribs)} contributors"

    def test_provenance_contributor_fields(self, real_profile):
        for t, contribs in real_profile["topic_provenance"].items():
            for c in contribs:
                assert c["kind"] in ("sub", "like")
                assert c["channel_name"]
                assert c["channel_id"]
                if c["kind"] == "sub":
                    assert c["subscribed_at"] is not None
                    assert c["liked_at"] is None
                if c["kind"] == "like":
                    assert c["liked_at"] is not None
                    assert c["video_title"] is not None
                    assert c["subscribed_at"] is None

    def test_broad_term_ranks_lower(self, real_profile):
        """The most pervasive broad tag ('music', 33/72 videos in probe) should
        rank below genre-specific music sub-tags in combined scores.

        This validates the core IDF suppression property: tags that appear on
        many entities get penalized relative to rarer, more informative tags.
        """
        cb = real_profile["combined_topic_scores"]
        if "music" not in cb:
            pytest.skip("music topic not in combined scores")
        genre_tags = [
            t for t in cb
            if t.endswith("-music") or t in ("jazz", "rhythm-and-blues")
        ]
        assert genre_tags, "Expected genre-specific music tags in profile"
        best_genre = max(genre_tags, key=lambda t: cb[t])
        assert cb[best_genre] > cb["music"], (
            f"Expected {best_genre} ({cb[best_genre]:.4f}) > music ({cb['music']:.4f})"
        )

    def test_temporal_comparison_visible(self, real_profile):
        """For some topic, recent > long_term or vice versa should be observable."""
        lt = real_profile["long_term_topic_scores"]
        rt = real_profile["recent_topic_scores"]
        overlap = set(lt.keys()) & set(rt.keys())
        # At least one topic where the two windows differ
        if overlap:
            diffs = [(t, abs(lt[t] - rt[t])) for t in overlap]
            max_diff = max(diffs, key=lambda x: x[1])
            assert max_diff[1] > 0.001, "Expected visible divergence between windows"


# ── extract_profile on synthetic minimal data ────────────────────────


class TestExtractProfileSynthetic:
    def test_empty_inputs(self):
        now = datetime(2026, 4, 16, tzinfo=timezone.utc)
        profile = extract_profile([], [], {}, {}, now)
        assert profile["long_term_topic_scores"] == {}
        assert profile["recent_topic_scores"] == {}
        assert profile["combined_topic_scores"] == {}
        assert profile["topic_provenance"] == {}
        assert profile["stats"]["total_subs"] == 0
        assert profile["stats"]["total_likes"] == 0

    def test_subs_only(self):
        now = datetime(2026, 4, 16, tzinfo=timezone.utc)
        subs = [
            {"snippet": {"resourceId": {"channelId": "C1"}, "publishedAt": "2020-01-01T00:00:00Z", "title": "Ch1"}},
            {"snippet": {"resourceId": {"channelId": "C2"}, "publishedAt": "2021-01-01T00:00:00Z", "title": "Ch2"}},
        ]
        channel_topics = {
            "C1": ["https://en.wikipedia.org/wiki/Jazz"],
            "C2": ["https://en.wikipedia.org/wiki/Jazz", "https://en.wikipedia.org/wiki/Music"],
        }
        profile = extract_profile(subs, [], channel_topics, {}, now)
        assert "jazz" in profile["long_term_topic_scores"]
        assert profile["recent_topic_scores"] == {}
        # Combined should equal long_term (alpha=0 when no likes)
        assert set(profile["combined_topic_scores"].keys()) == set(profile["long_term_topic_scores"].keys())
        assert sum(profile["combined_topic_scores"].values()) == pytest.approx(1.0, abs=1e-9)

    def test_likes_only(self):
        now = datetime(2026, 4, 16, tzinfo=timezone.utc)
        likes = [
            {
                "snippet": {
                    "publishedAt": "2026-04-10T00:00:00Z",
                    "videoOwnerChannelId": "C1",
                    "videoOwnerChannelTitle": "Ch1",
                    "title": "Video1",
                    "resourceId": {"videoId": "V1"},
                },
                "contentDetails": {"videoId": "V1"},
            },
        ]
        video_topics = {"V1": ["jazz"]}
        profile = extract_profile([], likes, {}, video_topics, now)
        assert profile["long_term_topic_scores"] == {}
        assert "jazz" in profile["recent_topic_scores"]
        assert "jazz" in profile["combined_topic_scores"]

    def test_provenance_fill_rule(self):
        """When one side is short, fill from the other continuing same sort order."""
        now = datetime(2026, 4, 16, tzinfo=timezone.utc)
        # 5 subs all tagged jazz, 0 likes → sub-only, should take up to 5 subs
        subs = [
            {"snippet": {"resourceId": {"channelId": f"C{i}"}, "publishedAt": f"202{i}-01-01T00:00:00Z", "title": f"Ch{i}"}}
            for i in range(5)
        ]
        channel_topics = {f"C{i}": ["https://en.wikipedia.org/wiki/Jazz"] for i in range(5)}
        profile = extract_profile(subs, [], channel_topics, {}, now)
        jazz_prov = profile["topic_provenance"]["jazz"]
        assert len(jazz_prov) == 5
        assert all(c["kind"] == "sub" for c in jazz_prov)
        # Should be in ascending subscribed_at order
        dates = [c["subscribed_at"] for c in jazz_prov]
        assert dates == sorted(dates)

    def test_blend_alpha_zero_no_likes(self):
        """With no likes, alpha=0, combined = long_term."""
        now = datetime(2026, 4, 16, tzinfo=timezone.utc)
        subs = [
            {"snippet": {"resourceId": {"channelId": "C1"}, "publishedAt": "2020-01-01T00:00:00Z", "title": "Ch1"}},
        ]
        channel_topics = {"C1": ["https://en.wikipedia.org/wiki/Jazz"]}
        profile = extract_profile(subs, [], channel_topics, {}, now)
        for t in profile["long_term_topic_scores"]:
            assert profile["combined_topic_scores"][t] == pytest.approx(
                profile["long_term_topic_scores"][t], abs=1e-9
            )

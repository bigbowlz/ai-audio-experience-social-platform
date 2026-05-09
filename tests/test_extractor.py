"""Tests for agents/youtube/extractor.py.

Synthetic-first: most tests use small synthetic inputs so they don't
depend on the disk cache or network. Real-data tests under TestExtractProfileReal
use the youtube/external agent loaders which canonicalize URLs to QIDs via
the committed agents/youtube/topic_cache.json.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agents.youtube.extractor import (
    InterestProfile,
    extract_profile,
    _decayed_weight,
    RECENT_HALF_LIFE_DAYS,
)
from agents.youtube.agent import _load_probe_data

PROBE_DIR = Path(__file__).resolve().parent.parent / "ydata" / "user"


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


# ── extract_profile on real probe data (loader canonicalizes URLs → QIDs) ──


@pytest.fixture
def now():
    return datetime(2026, 4, 16, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def real_profile(now):
    subs, likes, channel_qids, video_qids, topic_meta = _load_probe_data(PROBE_DIR)
    return extract_profile(
        subs, likes, channel_qids, video_qids, now, topic_meta=topic_meta,
    )


class TestExtractProfileReal:
    def test_stats_counts(self, real_profile):
        # ydata/user is a small dev probe; just assert shape and non-negativity.
        assert real_profile["stats"]["total_subs"] >= 0
        assert real_profile["stats"]["total_likes"] >= 0
        assert real_profile["stats"]["unique_topics"] > 0

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

    def test_topic_keys_are_qids(self, real_profile):
        """All scoring/provenance keys should be Wikidata QIDs (Q...)."""
        for key in real_profile["combined_topic_scores"].keys():
            assert key.startswith("Q") and key[1:].isdigit(), f"bad QID: {key}"
        for key in real_profile["topic_provenance"].keys():
            assert key.startswith("Q") and key[1:].isdigit()

    def test_topic_meta_present_for_top_topics(self, real_profile):
        """Every topic in the profile should have a label in topic_meta."""
        for q in real_profile["combined_topic_scores"]:
            assert q in real_profile["topic_meta"], f"missing meta for {q}"
            assert real_profile["topic_meta"][q]["label"]
            assert real_profile["topic_meta"][q]["canonical_url"].startswith("https://")

    def test_temporal_comparison_visible(self, real_profile):
        """For some topic, recent > long_term or vice versa should be observable."""
        lt = real_profile["long_term_topic_scores"]
        rt = real_profile["recent_topic_scores"]
        overlap = set(lt.keys()) & set(rt.keys())
        if overlap:
            diffs = [(t, abs(lt[t] - rt[t])) for t in overlap]
            max_diff = max(diffs, key=lambda x: x[1])
            assert max_diff[1] > 0.001, "Expected visible divergence between windows"


# ── extract_profile on synthetic minimal data ────────────────────────


def _sub(channel_id: str, year: int = 2020) -> dict:
    return {
        "snippet": {
            "resourceId": {"channelId": channel_id},
            "publishedAt": f"{year}-01-01T00:00:00Z",
            "title": f"Channel {channel_id}",
        }
    }


def _like(video_id: str, channel_id: str, days_ago: int = 6) -> dict:
    """Build a like roughly `days_ago` from 2026-04-16."""
    base = datetime(2026, 4, 16, tzinfo=timezone.utc)
    from datetime import timedelta
    when = (base - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "snippet": {
            "publishedAt": when,
            "videoOwnerChannelId": channel_id,
            "videoOwnerChannelTitle": f"Ch{channel_id}",
            "title": f"Vid {video_id}",
            "resourceId": {"videoId": video_id},
        },
        "contentDetails": {"videoId": video_id},
    }


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
        subs = [_sub("C1", 2020), _sub("C2", 2021)]
        channel_qids = {"C1": ["Q123"], "C2": ["Q123", "Q456"]}
        profile = extract_profile(subs, [], channel_qids, {}, now)
        assert "Q123" in profile["long_term_topic_scores"]
        assert profile["recent_topic_scores"] == {}
        # Combined should equal long_term (alpha=0 when no likes)
        assert set(profile["combined_topic_scores"].keys()) == set(
            profile["long_term_topic_scores"].keys()
        )
        assert sum(profile["combined_topic_scores"].values()) == pytest.approx(1.0, abs=1e-9)

    def test_likes_only(self):
        now = datetime(2026, 4, 16, tzinfo=timezone.utc)
        likes = [_like("V1", "C1")]
        video_qids = {"V1": ["Q123"]}
        profile = extract_profile([], likes, {}, video_qids, now)
        assert profile["long_term_topic_scores"] == {}
        assert "Q123" in profile["recent_topic_scores"]
        assert "Q123" in profile["combined_topic_scores"]

    def test_fractional_counting_length_penalty(self):
        """Topic in a 1-tag video gets weight 1, in a 3-tag video gets 1/3."""
        now = datetime(2026, 4, 16, tzinfo=timezone.utc)
        # Two likes, both ~today (so decay ≈ 1):
        # V1 has just Q-MUSIC (single tag), V2 has Q-MUSIC + 2 other tags.
        likes = [_like("V1", "C1", days_ago=0), _like("V2", "C2", days_ago=0)]
        video_qids = {"V1": ["Q-MUSIC"], "V2": ["Q-MUSIC", "Q-X", "Q-Y"]}
        profile = extract_profile([], likes, {}, video_qids, now)

        # Pre-normalization weights:
        #   Q-MUSIC: 1.0 + 1/3   ≈ 1.333
        #   Q-X:           1/3   ≈ 0.333
        #   Q-Y:           1/3   ≈ 0.333
        # After L1 normalize:
        #   Q-MUSIC ≈ 1.333 / 2.0 = 0.6667
        #   Q-X / Q-Y each ≈ 0.1667
        rec = profile["recent_topic_scores"]
        assert rec["Q-MUSIC"] == pytest.approx(2 / 3, abs=1e-3)
        assert rec["Q-X"] == pytest.approx(1 / 6, abs=1e-3)
        assert rec["Q-Y"] == pytest.approx(1 / 6, abs=1e-3)

    def test_provenance_fill_rule(self):
        """When one side is short, fill from the other continuing same sort order."""
        now = datetime(2026, 4, 16, tzinfo=timezone.utc)
        subs = [_sub(f"C{i}", 2020 + i) for i in range(5)]
        channel_qids = {f"C{i}": ["Q-JAZZ"] for i in range(5)}
        profile = extract_profile(subs, [], channel_qids, {}, now)
        prov = profile["topic_provenance"]["Q-JAZZ"]
        assert len(prov) == 5
        assert all(c["kind"] == "sub" for c in prov)
        # Should be in ascending subscribed_at order
        dates = [c["subscribed_at"] for c in prov]
        assert dates == sorted(dates)

    def test_blend_alpha_zero_no_likes(self):
        """With no likes, alpha=0, combined = long_term."""
        now = datetime(2026, 4, 16, tzinfo=timezone.utc)
        subs = [_sub("C1")]
        channel_qids = {"C1": ["Q-JAZZ"]}
        profile = extract_profile(subs, [], channel_qids, {}, now)
        for t in profile["long_term_topic_scores"]:
            assert profile["combined_topic_scores"][t] == pytest.approx(
                profile["long_term_topic_scores"][t], abs=1e-9
            )

    def test_topic_meta_passes_through(self):
        now = datetime(2026, 4, 16, tzinfo=timezone.utc)
        subs = [_sub("C1")]
        channel_qids = {"C1": ["Q123"]}
        topic_meta = {
            "Q123": {"label": "Jazz", "canonical_url": "https://en.wikipedia.org/wiki/Jazz"}
        }
        profile = extract_profile(subs, [], channel_qids, {}, now, topic_meta=topic_meta)
        assert profile["topic_meta"]["Q123"]["label"] == "Jazz"

    def test_topic_meta_pruned_to_present_topics(self):
        """topic_meta in the output should only contain QIDs that scored."""
        now = datetime(2026, 4, 16, tzinfo=timezone.utc)
        subs = [_sub("C1")]
        channel_qids = {"C1": ["Q123"]}
        topic_meta = {
            "Q123": {"label": "Jazz", "canonical_url": "https://en.wikipedia.org/wiki/Jazz"},
            "Q-UNUSED": {"label": "Other", "canonical_url": "https://en.wikipedia.org/wiki/Other"},
        }
        profile = extract_profile(subs, [], channel_qids, {}, now, topic_meta=topic_meta)
        assert "Q123" in profile["topic_meta"]
        assert "Q-UNUSED" not in profile["topic_meta"]

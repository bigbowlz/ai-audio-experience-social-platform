"""Tests for agents/youtube/guardrails.py.

Coverage per prompt_design.md §Test mandate:
- compute_claim_kind(): all 4 kinds + evaluation order + jazz-from-one-old-like + total_recent_weight floor
- compute_provenance_shape(): all 3 shapes
"""

from __future__ import annotations

import pytest

from agents.youtube.guardrails import (
    ClaimKind,
    ProvenanceShape,
    compute_claim_kind,
    compute_provenance_shape,
)


def _sub(name="Ch", cid="C1", subscribed_at="2020-01-01T00:00:00Z"):
    return {
        "kind": "sub",
        "channel_name": name,
        "channel_id": cid,
        "subscribed_at": subscribed_at,
        "liked_at": None,
        "video_title": None,
        "video_id": None,
    }


def _like(name="Ch", cid="C1", liked_at="2026-04-10T00:00:00Z", video_title="V", video_id="V1"):
    return {
        "kind": "like",
        "channel_name": name,
        "channel_id": cid,
        "subscribed_at": None,
        "liked_at": liked_at,
        "video_title": video_title,
        "video_id": video_id,
    }


# ── compute_claim_kind ───────────────────────────────────────────────


class TestComputeClaimKind:
    def test_rising(self):
        """rising: long_term > 0, recent > long_term, like_count >= 3, total_recent_weight >= 2.0."""
        prov = [_sub(), _sub(cid="C2"), _like(video_id="V1"), _like(video_id="V2"), _like(video_id="V3")]
        result = compute_claim_kind(
            topic="jazz",
            long_term={"jazz": 0.1},
            recent={"jazz": 0.3},
            provenance=prov,
            total_recent_weight=5.0,
        )
        assert result == ClaimKind.RISING

    def test_discovery(self):
        """discovery: long_term == 0, like_count >= 2."""
        prov = [_like(video_id="V1"), _like(video_id="V2")]
        result = compute_claim_kind(
            topic="anime",
            long_term={},
            recent={"anime": 0.5},
            provenance=prov,
            total_recent_weight=3.0,
        )
        assert result == ClaimKind.DISCOVERY

    def test_durable(self):
        """durable: long_term > 0, sub_count >= 2."""
        prov = [_sub(cid="C1"), _sub(cid="C2")]
        result = compute_claim_kind(
            topic="jazz",
            long_term={"jazz": 0.2},
            recent={},
            provenance=prov,
            total_recent_weight=0.0,
        )
        assert result == ClaimKind.DURABLE

    def test_neutral_fallback(self):
        """neutral: none of the above hold."""
        prov = [_sub()]  # only 1 sub, no likes
        result = compute_claim_kind(
            topic="jazz",
            long_term={"jazz": 0.1},
            recent={},
            provenance=prov,
            total_recent_weight=0.0,
        )
        assert result == ClaimKind.NEUTRAL

    def test_evaluation_order_rising_before_durable(self):
        """rising is checked first — if rising conditions hold, durable conditions
        also hold (lt > 0, sub_count >= 2) but rising wins."""
        prov = [_sub(cid="C1"), _sub(cid="C2"), _like(video_id="V1"), _like(video_id="V2"), _like(video_id="V3")]
        result = compute_claim_kind(
            topic="jazz",
            long_term={"jazz": 0.1},
            recent={"jazz": 0.3},
            provenance=prov,
            total_recent_weight=5.0,
        )
        assert result == ClaimKind.RISING  # not DURABLE

    def test_jazz_from_one_old_like(self):
        """The jazz-from-one-old-like scenario from prompt_design.md §1:
        1 like, no subs → should be NEUTRAL (fails rising, discovery, durable)."""
        prov = [_like(liked_at="2025-10-01T00:00:00Z")]
        result = compute_claim_kind(
            topic="jazz",
            long_term={},
            recent={"jazz": 0.05},
            provenance=prov,
            total_recent_weight=0.2,
        )
        assert result == ClaimKind.NEUTRAL

    def test_total_recent_weight_floor_on_rising(self):
        """3 likes from 2 years ago: like_count >= 3 but total_recent_weight < 2.0
        → should NOT be rising."""
        prov = [_sub(), _like(video_id="V1"), _like(video_id="V2"), _like(video_id="V3")]
        result = compute_claim_kind(
            topic="jazz",
            long_term={"jazz": 0.1},
            recent={"jazz": 0.3},
            provenance=prov,
            total_recent_weight=0.2,  # 3 old likes, decayed to ~0.2
        )
        # Fails rising (total_recent_weight < 2.0), has 1 sub so fails durable (need 2)
        assert result == ClaimKind.NEUTRAL

    def test_discovery_needs_two_likes(self):
        """1 like with lt == 0 → NEUTRAL, not discovery."""
        prov = [_like()]
        result = compute_claim_kind(
            topic="anime",
            long_term={},
            recent={"anime": 0.5},
            provenance=prov,
            total_recent_weight=1.0,
        )
        assert result == ClaimKind.NEUTRAL

    def test_durable_needs_two_subs(self):
        """1 sub with lt > 0 → NEUTRAL, not durable."""
        prov = [_sub()]
        result = compute_claim_kind(
            topic="jazz",
            long_term={"jazz": 0.2},
            recent={},
            provenance=prov,
            total_recent_weight=0.0,
        )
        assert result == ClaimKind.NEUTRAL


# ── compute_provenance_shape ─────────────────────────────────────────


class TestComputeProvenanceShape:
    def test_balanced(self):
        prov = [_sub(), _like()]
        assert compute_provenance_shape(prov) == ProvenanceShape.BALANCED

    def test_sub_only(self):
        prov = [_sub(), _sub(cid="C2")]
        assert compute_provenance_shape(prov) == ProvenanceShape.SUB_ONLY

    def test_like_only(self):
        prov = [_like(), _like(video_id="V2")]
        assert compute_provenance_shape(prov) == ProvenanceShape.LIKE_ONLY

    def test_empty_provenance(self):
        """Edge case: empty provenance → BALANCED (no evidence = no skew)."""
        assert compute_provenance_shape([]) == ProvenanceShape.BALANCED

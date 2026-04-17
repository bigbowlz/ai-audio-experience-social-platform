"""Tests for audio SSE event payload types.

Spec: audio/docs/DESIGN.md §SSE Event Contracts
"""

from audio.events import SegmentDone, SegmentDelayed, EpisodeDone, EpisodeFailed


class TestSegmentDone:
    def test_to_dict(self):
        evt = SegmentDone(segment_index=0, duration_ms=32000, url="/audio/ep1/segment_0.mp3")
        d = evt.to_dict()
        assert d == {
            "segment_index": 0,
            "duration_ms": 32000,
            "url": "/audio/ep1/segment_0.mp3",
        }


class TestSegmentDelayed:
    def test_to_dict_with_unknown_eta(self):
        evt = SegmentDelayed(segment_index=2, eta_ms=-1)
        d = evt.to_dict()
        assert d == {"segment_index": 2, "eta_ms": -1}


class TestEpisodeDone:
    def test_to_dict(self):
        evt = EpisodeDone(total_segments=5, skipped_segments=[2])
        d = evt.to_dict()
        assert d == {"total_segments": 5, "skipped_segments": [2]}

    def test_no_skipped(self):
        evt = EpisodeDone(total_segments=4, skipped_segments=[])
        assert evt.to_dict()["skipped_segments"] == []


class TestEpisodeFailed:
    def test_to_dict(self):
        evt = EpisodeFailed(reason="All segments failed after retries")
        assert evt.to_dict() == {"reason": "All segments failed after retries"}

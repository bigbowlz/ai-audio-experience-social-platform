"""SSE event payload types for the audio pipeline.

These define the payload shapes. Wire format and actual SSE emission
are owned by api-storage (not yet finalized).

Spec: audio/docs/DESIGN.md §SSE Event Contracts
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SegmentDone:
    """Emitted when a segment MP3 is fully written to disk."""
    segment_index: int
    duration_ms: int
    url: str

    def to_dict(self) -> dict:
        return {
            "segment_index": self.segment_index,
            "duration_ms": self.duration_ms,
            "url": self.url,
        }


@dataclass(frozen=True, slots=True)
class SegmentDelayed:
    """Emitted when player's segment queue underruns.

    eta_ms is telemetry only; -1 if unknown.
    Owned by the player (client-side detection).
    """
    segment_index: int
    eta_ms: int

    def to_dict(self) -> dict:
        return {
            "segment_index": self.segment_index,
            "eta_ms": self.eta_ms,
        }


@dataclass(frozen=True, slots=True)
class EpisodeDone:
    """Emitted when all segments have been processed."""
    total_segments: int
    skipped_segments: list[int]

    def to_dict(self) -> dict:
        return {
            "total_segments": self.total_segments,
            "skipped_segments": self.skipped_segments,
        }


@dataclass(frozen=True, slots=True)
class EpisodeFailed:
    """Emitted when all segments failed or pipeline timeout exceeded."""
    reason: str

    def to_dict(self) -> dict:
        return {"reason": self.reason}

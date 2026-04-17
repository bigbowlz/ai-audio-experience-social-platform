"""Tests for audio orchestrator — parallel dispatch + error handling.

Spec: audio/docs/DESIGN.md §Parallel dispatch, §Error Handling Matrix
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from audio.orchestrator import generate_episode_audio, AudioResult
from audio.tts import SegmentResult
from audio.events import SegmentDone, EpisodeDone, EpisodeFailed
from audio.config import VOICE_MAP


def _make_segment(agent: str, script: str, index: int) -> dict:
    """Create a minimal SegmentScript-like dict."""
    return {
        "agent": agent,
        "pitch_title": f"Title {index}",
        "segue_in": "" if index == 0 else "And now...",
        "script": script,
        "estimated_length_sec": 60,
    }


def _make_result(index: int, duration_ms: int = 30000) -> SegmentResult:
    return SegmentResult(
        segment_index=index,
        url=f"/audio/ep1/segment_{index}.mp3",
        duration_ms=duration_ms,
        duration_estimated=False,
        generation_time_ms=950,
        character_count=100,
        billed_character_count=100,
    )


class TestGenerateEpisodeAudio:
    @pytest.mark.asyncio
    async def test_all_segments_synthesized(self):
        """All segments produce results and episode_done event."""
        mock_tts = AsyncMock()
        mock_tts.synthesize = AsyncMock(side_effect=[
            _make_result(0),
            _make_result(1),
            _make_result(2),
        ])
        segments = [
            _make_segment("youtube", "Jazz talk", 0),
            _make_segment("weather", "Sunny day", 1),
            _make_segment("calendar", "Meeting at 10", 2),
        ]
        result = await generate_episode_audio(
            tts=mock_tts,
            segments=segments,
            episode_id="ep1",
        )
        assert len(result.segment_results) == 3
        assert result.episode_done is not None
        assert result.episode_done.total_segments == 3
        assert result.episode_done.skipped_segments == []
        assert result.episode_failed is None

    @pytest.mark.asyncio
    async def test_segment_1_fires_first(self):
        """Segment 0 must complete before segments 1+ start."""
        call_order = []

        async def mock_synth(text, voice_id, episode_id, segment_index):
            call_order.append(segment_index)
            if segment_index == 0:
                await asyncio.sleep(0.01)  # simulate generation time
            return _make_result(segment_index)

        mock_tts = AsyncMock()
        mock_tts.synthesize = AsyncMock(side_effect=mock_synth)

        segments = [
            _make_segment("youtube", "First", 0),
            _make_segment("weather", "Second", 1),
        ]
        await generate_episode_audio(tts=mock_tts, segments=segments, episode_id="ep1")
        # Segment 0 must be the first call
        assert call_order[0] == 0

    @pytest.mark.asyncio
    async def test_skips_failed_segment(self):
        """A segment that raises is skipped, not fatal."""
        async def mock_synth(text, voice_id, episode_id, segment_index):
            if segment_index == 1:
                raise Exception("422 Unprocessable")
            return _make_result(segment_index)

        mock_tts = AsyncMock()
        mock_tts.synthesize = AsyncMock(side_effect=mock_synth)

        segments = [
            _make_segment("youtube", "OK", 0),
            _make_segment("weather", "Bad text", 1),
            _make_segment("calendar", "OK too", 2),
        ]
        result = await generate_episode_audio(
            tts=mock_tts, segments=segments, episode_id="ep1",
        )
        assert len(result.segment_results) == 2
        assert result.episode_done.skipped_segments == [1]

    @pytest.mark.asyncio
    async def test_all_fail_emits_episode_failed(self):
        """When ALL segments fail, emit episode_failed."""
        mock_tts = AsyncMock()
        mock_tts.synthesize = AsyncMock(side_effect=Exception("API down"))

        segments = [_make_segment("youtube", "Fail", 0)]
        result = await generate_episode_audio(
            tts=mock_tts, segments=segments, episode_id="ep1",
        )
        assert result.episode_failed is not None
        assert result.episode_done is None

    @pytest.mark.asyncio
    async def test_voice_map_lookup(self):
        """Orchestrator resolves voice_id from VOICE_MAP per segment agent."""
        mock_tts = AsyncMock()
        mock_tts.synthesize = AsyncMock(return_value=_make_result(0))

        segments = [_make_segment("alices", "Alice's take", 0)]
        await generate_episode_audio(
            tts=mock_tts, segments=segments, episode_id="ep1",
        )
        call_kwargs = mock_tts.synthesize.call_args
        # alices agent should use GUEST_VOICE_ID
        assert call_kwargs.kwargs["voice_id"] == VOICE_MAP["alices"]

    @pytest.mark.asyncio
    async def test_collects_billed_characters(self):
        """AudioResult tracks total billed characters across all segments."""
        mock_tts = AsyncMock()
        mock_tts.synthesize = AsyncMock(side_effect=[
            _make_result(0),
            _make_result(1),
        ])
        segments = [
            _make_segment("youtube", "A" * 100, 0),
            _make_segment("weather", "B" * 100, 1),
        ]
        result = await generate_episode_audio(
            tts=mock_tts, segments=segments, episode_id="ep1",
        )
        assert result.total_billed_characters == 200

"""Tests for audio orchestrator — parallel dispatch + error handling.

Spec: audio/docs/DESIGN.md §Parallel dispatch, §Error Handling Matrix
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest


async def _as_stream(items: list) -> AsyncIterator:
    for item in items:
        yield item

from audio.orchestrator import generate_episode_audio, AudioResult
from audio.tts import SegmentResult
from audio.events import SegmentDone, EpisodeDone, EpisodeFailed
from unittest.mock import patch

from audio.config import VOICE_MAP, NARRATOR_VOICE_ID
import audio.orchestrator as _orch


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
            segments=_as_stream(segments),
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
        await generate_episode_audio(tts=mock_tts, segments=_as_stream(segments), episode_id="ep1")
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
            tts=mock_tts, segments=_as_stream(segments), episode_id="ep1",
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
            tts=mock_tts, segments=_as_stream(segments), episode_id="ep1",
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
            tts=mock_tts, segments=_as_stream(segments), episode_id="ep1",
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
            tts=mock_tts, segments=_as_stream(segments), episode_id="ep1",
        )
        assert result.total_billed_characters == 200

    @pytest.mark.asyncio
    async def test_pipeline_timeout_emits_episode_failed(self):
        """When pipeline timeout is hit, episode_failed should be emitted."""
        async def slow_synth(text, voice_id, episode_id, segment_index):
            await asyncio.sleep(999)
            return _make_result(segment_index)

        mock_tts = AsyncMock()
        mock_tts.synthesize = AsyncMock(side_effect=slow_synth)

        segments = [_make_segment("youtube", "Slow", 0)]
        with patch.object(_orch, "PIPELINE_TIMEOUT_SEC", 0.01):
            result = await generate_episode_audio(
                tts=mock_tts, segments=_as_stream(segments), episode_id="ep1",
            )
        assert result.episode_failed is not None
        assert result.episode_done is None

    @pytest.mark.asyncio
    async def test_unknown_agent_uses_narrator_voice(self):
        """Agent not in VOICE_MAP falls back to NARRATOR_VOICE_ID."""
        mock_tts = AsyncMock()
        mock_tts.synthesize = AsyncMock(return_value=_make_result(0))

        segments = [_make_segment("unknown_agent", "Hello", 0)]
        await generate_episode_audio(
            tts=mock_tts, segments=_as_stream(segments), episode_id="ep1",
        )
        call_kwargs = mock_tts.synthesize.call_args
        assert call_kwargs.kwargs["voice_id"] == NARRATOR_VOICE_ID

    @pytest.mark.asyncio
    async def test_empty_segments_emits_episode_failed(self):
        """Empty segments list produces episode_failed."""
        mock_tts = AsyncMock()
        result = await generate_episode_audio(
            tts=mock_tts, segments=_as_stream([]), episode_id="ep1",
        )
        assert result.episode_failed is not None
        assert result.episode_done is None


class TestOnSegmentDoneCallback:
    """Spec: audio/docs/DESIGN.md §Parallel dispatch — real-time SSE emission."""

    @pytest.mark.asyncio
    async def test_callback_fires_per_segment(self):
        """on_segment_done fires once per successful segment, in completion order."""
        mock_tts = AsyncMock()
        mock_tts.synthesize = AsyncMock(side_effect=[
            _make_result(0),
            _make_result(1),
            _make_result(2),
        ])
        callback_events: list[SegmentDone] = []

        async def on_done(event: SegmentDone) -> None:
            callback_events.append(event)

        segments = [
            _make_segment("youtube", "A", 0),
            _make_segment("weather", "B", 1),
            _make_segment("calendar", "C", 2),
        ]
        await generate_episode_audio(
            tts=mock_tts, segments=_as_stream(segments), episode_id="ep1",
            on_segment_done=on_done,
        )
        assert len(callback_events) == 3
        # Segment 0 always fires first (critical path)
        assert callback_events[0].segment_index == 0

    @pytest.mark.asyncio
    async def test_callback_fires_for_seg0_before_background(self):
        """Segment 0's callback fires before any background segment completes."""
        fired_at: dict[int, int] = {}
        counter = 0

        async def on_done(event: SegmentDone) -> None:
            nonlocal counter
            fired_at[event.segment_index] = counter
            counter += 1

        async def mock_synth(text, voice_id, episode_id, segment_index):
            if segment_index == 0:
                await asyncio.sleep(0.01)
            return _make_result(segment_index)

        mock_tts = AsyncMock()
        mock_tts.synthesize = AsyncMock(side_effect=mock_synth)

        segments = [
            _make_segment("youtube", "First", 0),
            _make_segment("weather", "Second", 1),
        ]
        await generate_episode_audio(
            tts=mock_tts, segments=_as_stream(segments), episode_id="ep1",
            on_segment_done=on_done,
        )
        assert fired_at[0] == 0  # seg 0 callback fired first

    @pytest.mark.asyncio
    async def test_callback_not_fired_for_failed_segment(self):
        """Failed segments do not trigger the callback."""
        callback_events: list[SegmentDone] = []

        async def on_done(event: SegmentDone) -> None:
            callback_events.append(event)

        async def mock_synth(text, voice_id, episode_id, segment_index):
            if segment_index == 1:
                raise Exception("422 Unprocessable")
            return _make_result(segment_index)

        mock_tts = AsyncMock()
        mock_tts.synthesize = AsyncMock(side_effect=mock_synth)

        segments = [
            _make_segment("youtube", "OK", 0),
            _make_segment("weather", "Bad", 1),
            _make_segment("calendar", "OK", 2),
        ]
        await generate_episode_audio(
            tts=mock_tts, segments=_as_stream(segments), episode_id="ep1",
            on_segment_done=on_done,
        )
        indices = [e.segment_index for e in callback_events]
        assert 1 not in indices
        assert len(callback_events) == 2

    @pytest.mark.asyncio
    async def test_no_callback_still_works(self):
        """Passing no callback (None) works — backwards compatible."""
        mock_tts = AsyncMock()
        mock_tts.synthesize = AsyncMock(return_value=_make_result(0))

        segments = [_make_segment("youtube", "Test", 0)]
        result = await generate_episode_audio(
            tts=mock_tts, segments=_as_stream(segments), episode_id="ep1",
        )
        assert len(result.segment_results) == 1


class TestFailFast401:
    """Spec: audio/docs/DESIGN.md §Error Handling Matrix — 401 aborts pipeline."""

    @pytest.mark.asyncio
    async def test_401_on_seg0_aborts_immediately(self):
        """401 on segment 0 stops the pipeline — no further segments attempted."""
        api_error = Exception("unauthorized")
        api_error.status_code = 401  # type: ignore[attr-defined]

        mock_tts = AsyncMock()
        mock_tts.synthesize = AsyncMock(side_effect=api_error)

        segments = [
            _make_segment("youtube", "A", 0),
            _make_segment("weather", "B", 1),
            _make_segment("calendar", "C", 2),
        ]
        result = await generate_episode_audio(
            tts=mock_tts, segments=_as_stream(segments), episode_id="ep1",
        )
        assert result.episode_failed is not None
        assert "401" in result.episode_failed.reason
        assert result.episode_done is None
        # Only segment 0 was attempted — segments 1-2 never called
        assert mock_tts.synthesize.call_count == 1

    @pytest.mark.asyncio
    async def test_401_on_background_seg_aborts(self):
        """401 on a background segment aborts remaining work."""
        call_count = 0

        async def mock_synth(text, voice_id, episode_id, segment_index):
            nonlocal call_count
            call_count += 1
            if segment_index == 0:
                return _make_result(0)
            api_error = Exception("unauthorized")
            api_error.status_code = 401  # type: ignore[attr-defined]
            raise api_error

        mock_tts = AsyncMock()
        mock_tts.synthesize = AsyncMock(side_effect=mock_synth)

        segments = [
            _make_segment("youtube", "OK", 0),
            _make_segment("weather", "Bad key", 1),
        ]
        result = await generate_episode_audio(
            tts=mock_tts, segments=_as_stream(segments), episode_id="ep1",
        )
        assert result.episode_failed is not None
        assert "401" in result.episode_failed.reason

    @pytest.mark.asyncio
    async def test_422_does_not_abort(self):
        """422 is per-segment skip, NOT pipeline abort."""
        api_error = Exception("unprocessable")
        api_error.status_code = 422  # type: ignore[attr-defined]

        async def mock_synth(text, voice_id, episode_id, segment_index):
            if segment_index == 1:
                raise api_error
            return _make_result(segment_index)

        mock_tts = AsyncMock()
        mock_tts.synthesize = AsyncMock(side_effect=mock_synth)

        segments = [
            _make_segment("youtube", "OK", 0),
            _make_segment("weather", "Bad", 1),
            _make_segment("calendar", "OK", 2),
        ]
        result = await generate_episode_audio(
            tts=mock_tts, segments=_as_stream(segments), episode_id="ep1",
        )
        # Pipeline continues — episode_done, not episode_failed
        assert result.episode_done is not None
        assert result.episode_failed is None
        assert result.episode_done.skipped_segments == [1]

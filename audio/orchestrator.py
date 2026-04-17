"""Audio orchestrator — parallel TTS dispatch with error handling.

Fires segment 0 first (critical path), then segments 1-N in parallel.
Collects results and emits episode-level events.

Spec: audio/docs/DESIGN.md §Parallel dispatch, §Error Handling Matrix
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from audio.config import VOICE_MAP, NARRATOR_VOICE_ID
from audio.events import EpisodeDone, EpisodeFailed, SegmentDone
from audio.tts import TTSClient, SegmentResult

logger = logging.getLogger(__name__)

# Total pipeline timeout (seconds). If zero segments succeed within this
# window, emit episode.failed.
PIPELINE_TIMEOUT_SEC = 120


@dataclass
class AudioResult:
    """Collected results from a full episode audio generation."""
    segment_results: list[SegmentResult] = field(default_factory=list)
    segment_done_events: list[SegmentDone] = field(default_factory=list)
    skipped_segments: list[int] = field(default_factory=list)
    episode_done: EpisodeDone | None = None
    episode_failed: EpisodeFailed | None = None
    total_billed_characters: int = 0


async def generate_episode_audio(
    tts: TTSClient,
    segments: list[dict],
    episode_id: str,
) -> AudioResult:
    """Generate audio for all segments in an episode.

    Segment 0 fires first (critical path — user is waiting).
    Segments 1-N fire in parallel after segment 0 completes.

    Each segment's voice_id is resolved via VOICE_MAP.
    Failed segments are skipped (added to skipped_segments).
    If ALL segments fail, episode_failed is emitted instead of episode_done.

    Args:
        tts: TTSClient instance (pre-configured with API key and output dir).
        segments: list of SegmentScript dicts from Producer.
        episode_id: unique episode identifier.

    Returns:
        AudioResult with all collected results and events.
    """
    result = AudioResult()
    total = len(segments)
    completed_indices: set[int] = set()

    async def _synth_one(seg: dict, index: int) -> None:
        agent = seg["agent"]
        voice_id = VOICE_MAP.get(agent, NARRATOR_VOICE_ID)
        try:
            seg_result = await tts.synthesize(
                text=seg["script"],
                voice_id=voice_id,
                episode_id=episode_id,
                segment_index=index,
            )
            result.segment_results.append(seg_result)
            result.segment_done_events.append(SegmentDone(
                segment_index=seg_result["segment_index"],
                duration_ms=seg_result["duration_ms"],
                url=seg_result["url"],
            ))
            result.total_billed_characters += seg_result["billed_character_count"]
            completed_indices.add(index)
        except Exception:
            logger.exception("Segment %d (%s) failed, skipping", index, agent)
            result.skipped_segments.append(index)

    try:
        async with asyncio.timeout(PIPELINE_TIMEOUT_SEC):
            # Segment 0 first (critical path)
            if segments:
                await _synth_one(segments[0], 0)

            # Segments 1-N in parallel
            if len(segments) > 1:
                tasks = [
                    asyncio.create_task(_synth_one(seg, i))
                    for i, seg in enumerate(segments[1:], start=1)
                ]
                await asyncio.gather(*tasks)
    except TimeoutError:
        logger.error("Pipeline timeout (%ds) exceeded", PIPELINE_TIMEOUT_SEC)

    # Determine terminal event
    if completed_indices:
        result.episode_done = EpisodeDone(
            total_segments=total,
            skipped_segments=sorted(result.skipped_segments),
        )
    else:
        result.episode_failed = EpisodeFailed(
            reason=f"All {total} segments failed or pipeline timeout exceeded",
        )

    return result

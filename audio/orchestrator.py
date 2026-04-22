"""Audio orchestrator — parallel TTS dispatch with error handling.

Fires segment 0 first (critical path), then segments 1-N in parallel.
Collects results and emits episode-level events.

Spec: audio/docs/DESIGN.md §Parallel dispatch, §Error Handling Matrix
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from audio.budget import record as record_budget
from audio.config import VOICE_MAP, NARRATOR_VOICE_ID, PIPELINE_TIMEOUT_SEC
from audio.events import EpisodeDone, EpisodeFailed, SegmentDone
from audio.tts import TTSClient, SegmentResult
from producer.script import SegmentScript

logger = logging.getLogger(__name__)

# Callback type: called with each SegmentDone event as segments complete.
# The callback receives the event immediately when a segment finishes,
# enabling real-time SSE emission and player playback start.
OnSegmentDone = Callable[[SegmentDone], Awaitable[None]]


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
    segments: AsyncIterator[SegmentScript],
    episode_id: str,
    on_segment_done: OnSegmentDone | None = None,
) -> AudioResult:
    """Generate audio for all segments in an episode.

    Segment 0 fires first (critical path — user is waiting).
    Segments 1-N dispatch as async tasks as the iterator yields them,
    enabling producer↔audio parallelism.
    The on_segment_done callback fires immediately as each segment
    finishes, enabling real-time SSE emission per the design spec.

    Each segment's voice_id is resolved via VOICE_MAP.
    Failed segments are skipped (added to skipped_segments).
    If ALL segments fail, episode_failed is emitted instead of episode_done.

    Args:
        tts: TTSClient instance (pre-configured with API key and output dir).
        segments: AsyncIterator of SegmentScript dicts from Producer.
        episode_id: unique episode identifier.
        on_segment_done: async callback fired per segment completion.
            Receives a SegmentDone event. Caller uses this to emit SSE
            and trigger player playback. None to skip (batch-return only).

    Returns:
        AudioResult with all collected results and events.
    """
    result = AudioResult()
    completed_indices: set[int] = set()

    async def _synth_one(seg: dict, index: int) -> None:
        for key in ("agent", "script"):
            if key not in seg:
                raise KeyError(
                    f"Segment {index} missing required key '{key}'. "
                    f"Got keys: {list(seg.keys())}"
                )
        agent = seg["agent"]
        voice_id = VOICE_MAP.get(agent, NARRATOR_VOICE_ID)
        # Producer pacing counts (segue_in + script) as the spoken unit — TTS
        # must match or drift telemetry lies and the LLM's transition prose
        # is wasted. Opener/sign_off pass segue_in="" and short-circuit.
        segue = seg.get("segue_in", "").strip()
        text = f"{segue} {seg['script']}" if segue else seg["script"]
        try:
            seg_result = await tts.synthesize(
                text=text,
                voice_id=voice_id,
                episode_id=episode_id,
                segment_index=index,
            )
            event = SegmentDone(
                segment_index=seg_result["segment_index"],
                duration_ms=seg_result["duration_ms"],
                url=seg_result["url"],
            )
            result.segment_results.append(seg_result)
            result.segment_done_events.append(event)
            result.total_billed_characters += seg_result["billed_character_count"]
            completed_indices.add(index)
            # Fire callback immediately so caller can emit SSE / start playback
            if on_segment_done is not None:
                await on_segment_done(event)
        except Exception as exc:
            # 401 = bad API key — unrecoverable, fail the entire pipeline
            # immediately instead of wasting time on remaining segments.
            if getattr(exc, "status_code", None) == 401:
                raise
            logger.exception("Segment %d (%s) failed, skipping", index, agent)
            result.skipped_segments.append(index)

    try:
        async with asyncio.timeout(PIPELINE_TIMEOUT_SEC):
            seg_iter = segments.__aiter__()

            try:
                seg_0 = await seg_iter.__anext__()
            except StopAsyncIteration:
                seg_0 = None

            if seg_0 is not None:
                await _synth_one(seg_0, 0)

                tasks: list[asyncio.Task] = []
                index = 1
                async for seg in seg_iter:
                    tasks.append(asyncio.create_task(_synth_one(seg, index)))
                    index += 1

                if tasks:
                    for coro in asyncio.as_completed(tasks):
                        await coro
    except TimeoutError:
        logger.error("Pipeline timeout (%ds) exceeded", PIPELINE_TIMEOUT_SEC)
    except Exception as exc:
        if getattr(exc, "status_code", None) == 401:
            logger.error("401 Unauthorized — invalid API key, aborting pipeline")
            result.episode_failed = EpisodeFailed(reason="Invalid API key (401)")
            return result
        raise

    # Sort results by segment index (parallel dispatch completes non-deterministically)
    result.segment_results.sort(key=lambda r: r["segment_index"])
    result.segment_done_events.sort(key=lambda e: e.segment_index)

    total = len(completed_indices) + len(result.skipped_segments)

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

    # Persist cumulative budget (warns at 80% of $20 ceiling)
    if result.total_billed_characters > 0:
        record_budget(result.total_billed_characters)

    return result

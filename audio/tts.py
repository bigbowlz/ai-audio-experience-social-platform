"""TTSClient — batch TTS via ElevenLabs SDK.

Synthesizes text to per-segment MP3 files on local disk.
Uses ElevenLabs Python SDK. Batch-only (streaming dropped for v0).

Spec: audio/docs/DESIGN.md §Interface contract, §Batch path, §Error Handling Matrix
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TypedDict

from elevenlabs.client import ElevenLabs
from mutagen.mp3 import MP3, HeaderNotFoundError

from audio.config import (
    ELEVENLABS_MODEL,
    MAX_RETRIES,
    OUTPUT_FORMAT,
    REQUEST_TIMEOUT_SEC,
    RETRY_BACKOFF_BASE_SEC,
    resolve_voice_settings,
)
from audio.pronunciation import apply_pronunciation

logger = logging.getLogger(__name__)

# Duration estimation fallback: ~13 chars per second of speech
# (~150 wpm * ~5 chars/word / 60 sec)
_CHARS_PER_SEC = 13

# HTTP status codes that warrant retry with exponential backoff
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503}


class SegmentResult(TypedDict):
    segment_index: int
    url: str                    # web route: "/audio/{episode_id}/segment_{segment_index}.mp3" (v1 frontend)
    audio_path: str             # on-disk path afplay/ffmpeg can open (v0 CLI)
    duration_ms: int            # audio duration, parsed from MP3 header via mutagen
    duration_estimated: bool    # True if mutagen failed and duration was estimated
    generation_time_ms: int     # wall-clock TTS generation time
    character_count: int        # chars in the successful request
    billed_character_count: int # total chars billed including failed retries


class TTSClient:
    """Synthesizes text to per-segment MP3 files on local disk.

    Uses ElevenLabs Python SDK. Batch-only.
    Concurrency bounded by max_concurrent semaphore.
    """

    def __init__(
        self,
        api_key: str,
        output_dir: str = "./data/episodes",
        model_id: str = ELEVENLABS_MODEL,
        max_concurrent: int = 4,
    ):
        self._client = ElevenLabs(api_key=api_key)
        self._output_dir = Path(output_dir)
        self._model_id = model_id
        self._max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        episode_id: str,
        segment_index: int,
    ) -> SegmentResult:
        """Synthesize text to MP3 and write to disk.

        Writes to {output_dir}/{episode_id}/segment_{segment_index}.mp3.
        Applies pronunciation rules before sending to ElevenLabs.
        Retries transient errors (429, 5xx) with exponential backoff.
        Per-request timeout: REQUEST_TIMEOUT_SEC (60s).

        Returns SegmentResult with URL, timing, and billing metadata.
        """
        async with self._semaphore:
            return await self._synthesize_batch(
                text, voice_id, episode_id, segment_index
            )

    async def _synthesize_batch(
        self,
        text: str,
        voice_id: str,
        episode_id: str,
        segment_index: int,
    ) -> SegmentResult:
        processed_text = apply_pronunciation(text)
        char_count = len(processed_text)
        billed_chars = 0
        voice_settings = resolve_voice_settings(voice_id)

        # Prepare output path
        episode_dir = self._output_dir / episode_id
        episode_dir.mkdir(parents=True, exist_ok=True)
        output_path = episode_dir / f"segment_{segment_index}.mp3"
        tmp_path = output_path.with_suffix(".mp3.tmp")

        # Retry loop with exponential backoff for transient errors
        last_error: Exception | None = None
        start = time.monotonic()

        for attempt in range(1 + MAX_RETRIES):
            billed_chars += char_count  # ElevenLabs bills per-attempt
            try:
                audio_chunks = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        None,
                        lambda: self._client.text_to_speech.convert(
                            voice_id=voice_id,
                            text=processed_text,
                            model_id=self._model_id,
                            voice_settings=voice_settings,
                            output_format=OUTPUT_FORMAT,
                        ),
                    ),
                    timeout=REQUEST_TIMEOUT_SEC,
                )
                break  # success
            except TimeoutError:
                last_error = TimeoutError(
                    f"Request timeout ({REQUEST_TIMEOUT_SEC}s) on attempt {attempt + 1}"
                )
                if attempt == 0:
                    # Network timeout: retry once per spec
                    logger.warning("Segment %d: timeout, retrying once", segment_index)
                    continue
                raise last_error
            except Exception as exc:
                last_error = exc
                if self._is_retryable(exc) and attempt < MAX_RETRIES:
                    delay = RETRY_BACKOFF_BASE_SEC * (2 ** attempt)
                    logger.warning(
                        "Segment %d: retryable error (attempt %d/%d), "
                        "backing off %.1fs: %s",
                        segment_index, attempt + 1, 1 + MAX_RETRIES, delay, exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
        else:
            raise last_error  # type: ignore[misc]

        gen_time_ms = int((time.monotonic() - start) * 1000)

        # Write to disk atomically (tmp → rename)
        try:
            with open(tmp_path, "wb") as f:
                for chunk in audio_chunks:
                    f.write(chunk)
            tmp_path.rename(output_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        # Parse duration via mutagen, fallback to char-count estimate
        duration_ms, estimated = self._parse_duration(output_path, char_count)

        return SegmentResult(
            segment_index=segment_index,
            url=f"/audio/{episode_id}/segment_{segment_index}.mp3",
            audio_path=str(output_path),
            duration_ms=duration_ms,
            duration_estimated=estimated,
            generation_time_ms=gen_time_ms,
            character_count=char_count,
            billed_character_count=billed_chars,
        )

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """Check if an exception from the ElevenLabs SDK is retryable."""
        # The ElevenLabs SDK raises ApiError with a status_code attribute
        status = getattr(exc, "status_code", None)
        if status and status in _RETRYABLE_STATUS_CODES:
            return True
        # Also retry on connection-level errors
        if isinstance(exc, (ConnectionError, OSError)):
            return True
        return False

    @staticmethod
    def _parse_duration(path: Path, char_count: int) -> tuple[int, bool]:
        """Parse MP3 duration via mutagen. Fallback: estimate from char count."""
        try:
            audio = MP3(str(path))
            if audio.info and audio.info.length > 0:
                return int(audio.info.length * 1000), False
        except Exception:
            pass
        # Fallback: ~150 chars/sec spoken
        estimated_ms = int(char_count / _CHARS_PER_SEC * 1000)
        return estimated_ms, True

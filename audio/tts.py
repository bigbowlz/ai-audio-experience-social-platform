"""TTSClient — batch TTS via ElevenLabs SDK.

Synthesizes text to per-segment MP3 files on local disk.
Uses ElevenLabs Python SDK. Batch-only (streaming dropped for v0).

Spec: audio/docs/DESIGN.md §Interface contract, §Batch path
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TypedDict

from elevenlabs.client import ElevenLabs
from mutagen.mp3 import MP3, HeaderNotFoundError

from audio.config import ELEVENLABS_MODEL, OUTPUT_FORMAT
from audio.pronunciation import apply_pronunciation

# Duration estimation fallback: ~150 chars per second of speech
_CHARS_PER_SEC = 150


class SegmentResult(TypedDict):
    segment_index: int
    url: str                    # "/audio/{episode_id}/segment_{segment_index}.mp3"
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

        # Prepare output path
        episode_dir = self._output_dir / episode_id
        episode_dir.mkdir(parents=True, exist_ok=True)
        output_path = episode_dir / f"segment_{segment_index}.mp3"
        tmp_path = output_path.with_suffix(".mp3.tmp")

        # Call ElevenLabs batch API (sync SDK call run in executor)
        start = time.monotonic()
        audio_chunks = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._client.text_to_speech.convert(
                voice_id=voice_id,
                text=processed_text,
                model_id=self._model_id,
                voice_settings={
                    "stability": 0.5,
                    "similarity_boost": 0.75,
                    "style": 0.0,
                    "use_speaker_boost": True,
                },
                output_format=OUTPUT_FORMAT,
            ),
        )
        gen_time_ms = int((time.monotonic() - start) * 1000)

        # Write to disk atomically (tmp → rename)
        with open(tmp_path, "wb") as f:
            for chunk in audio_chunks:
                f.write(chunk)
        tmp_path.rename(output_path)

        # Parse duration via mutagen, fallback to char-count estimate
        duration_ms, estimated = self._parse_duration(output_path, char_count)

        return SegmentResult(
            segment_index=segment_index,
            url=f"/audio/{episode_id}/segment_{segment_index}.mp3",
            duration_ms=duration_ms,
            duration_estimated=estimated,
            generation_time_ms=gen_time_ms,
            character_count=char_count,
            billed_character_count=char_count,
        )

    @staticmethod
    def _parse_duration(path: Path, char_count: int) -> tuple[int, bool]:
        """Parse MP3 duration via mutagen. Fallback: estimate from char count."""
        try:
            audio = MP3(str(path))
            if audio.info and audio.info.length > 0:
                return int(audio.info.length * 1000), False
        except (HeaderNotFoundError, Exception):
            pass
        # Fallback: ~150 chars/sec spoken
        estimated_ms = int(char_count / _CHARS_PER_SEC * 1000)
        return estimated_ms, True

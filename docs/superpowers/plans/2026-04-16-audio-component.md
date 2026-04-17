# Audio Component Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the audio component that turns Producer `SegmentScript` objects into listenable per-segment MP3 files on disk via ElevenLabs batch TTS, with pronunciation preprocessing, parallel dispatch, error handling, and offline concat.

**Architecture:** TTSClient wraps the ElevenLabs Python SDK for batch TTS synthesis. An orchestrator fires segment 1 first (critical path), then segments 2-N in parallel. Pronunciation rules are applied as a regex pass before sending text to ElevenLabs. Offline concat assembles a single-file MP3 via ffmpeg post-rehearsal. SSE event types are defined as dataclasses for downstream consumers but emission is deferred to api-storage integration.

**Tech Stack:** Python 3.14, `elevenlabs` SDK 2.43.0, `mutagen` 1.47.0 (MP3 duration parsing), `asyncio` (parallel dispatch), `ffmpeg` (offline concat), `pytest` (tests)

**Spec:** `audio/docs/DESIGN.md` (eng-reviewed 2026-04-16)

---

## File Structure

| File | Responsibility |
|------|---------------|
| `audio/__init__.py` | Package init, re-exports public API |
| `audio/pronunciation.py` | `PRONUNCIATION_RULES` list + `apply_pronunciation()` regex pass |
| `audio/tts.py` | `TTSClient` class — batch TTS via ElevenLabs SDK, disk write, duration parsing, `SegmentResult` type |
| `audio/events.py` | SSE event payload dataclasses (`SegmentDone`, `SegmentDelayed`, `EpisodeDone`, `EpisodeFailed`) |
| `audio/orchestrator.py` | `generate_episode_audio()` — parallel dispatch (seg 1 first, 2-N parallel), error handling, event collection |
| `audio/concat.py` | `concat_episode()` — offline ffmpeg single-file MP3 assembly |
| `audio/config.py` | Already exists — voice IDs, model, format, voice map |
| `tests/test_pronunciation.py` | Tests for pronunciation rules |
| `tests/test_tts.py` | Tests for TTSClient (mocked ElevenLabs SDK) |
| `tests/test_events.py` | Tests for event payload construction |
| `tests/test_orchestrator.py` | Tests for parallel dispatch logic + error handling |
| `tests/test_concat.py` | Tests for offline concat |

---

### Task 1: Pronunciation rules

**Files:**
- Create: `audio/pronunciation.py`
- Test: `tests/test_pronunciation.py`

- [ ] **Step 1: Write failing tests for pronunciation rules**

```python
# tests/test_pronunciation.py
"""Tests for audio pronunciation preprocessing.

Spec: audio/docs/DESIGN.md §SSML / Pronunciation handling
"""

from audio.pronunciation import apply_pronunciation


class TestApplyPronunciation:
    def test_strips_at_handles(self):
        assert apply_pronunciation("Follow @ofmiles") == "Follow ofmiles"

    def test_expands_cpi(self):
        assert apply_pronunciation("The CPI rose") == "The C P I rose"

    def test_expands_gdp(self):
        assert apply_pronunciation("GDP growth") == "G D P growth"

    def test_expands_ai(self):
        assert apply_pronunciation("AI is changing") == "A I is changing"

    def test_multiple_rules_applied(self):
        text = "AI and CPI data from @analyst"
        result = apply_pronunciation(text)
        assert result == "A I and C P I data from analyst"

    def test_no_match_passthrough(self):
        text = "Just a normal sentence about music."
        assert apply_pronunciation(text) == text

    def test_at_handle_mid_word_not_matched(self):
        # email addresses should not be stripped
        assert apply_pronunciation("user@example.com") == "user@example.com"

    def test_case_sensitive_acronyms(self):
        # lowercase "cpi" should NOT match
        assert apply_pronunciation("cpi is lowercase") == "cpi is lowercase"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/wanlizhou/Learn/Projects/radio-podcast && python -m pytest tests/test_pronunciation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'audio.pronunciation'`

- [ ] **Step 3: Create `audio/__init__.py`**

```python
# audio/__init__.py
```

Empty file — marks `audio/` as a Python package.

- [ ] **Step 4: Implement `apply_pronunciation`**

```python
# audio/pronunciation.py
"""Post-LLM pronunciation preprocessing.

Regex rules applied BEFORE sending text to ElevenLabs.
The list grows during rehearsal listen-throughs.

Spec: audio/docs/DESIGN.md §SSML / Pronunciation handling
"""

from __future__ import annotations

import re

# (pattern, replacement) — applied in order.
# Patterns use word boundaries or lookahead to avoid false positives.
PRONUNCIATION_RULES: list[tuple[str, str]] = [
    (r"(?<!\w)@(\w+)", r"\1"),       # strip @ from handles (not emails)
    (r"\bCPI\b", "C P I"),
    (r"\bGDP\b", "G D P"),
    (r"\bAI\b", "A I"),
]


def apply_pronunciation(text: str) -> str:
    """Apply all pronunciation rules to text.

    Called on every segment's script text before TTS synthesis,
    regardless of voice or speaker.
    """
    for pattern, replacement in PRONUNCIATION_RULES:
        text = re.sub(pattern, replacement, text)
    return text
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/wanlizhou/Learn/Projects/radio-podcast && python -m pytest tests/test_pronunciation.py -v`
Expected: All 8 tests PASS

- [ ] **Step 6: Commit**

```bash
git add audio/__init__.py audio/pronunciation.py tests/test_pronunciation.py
git commit -m "feat(audio): pronunciation rules with regex preprocessing"
```

---

### Task 2: SSE event payload types

**Files:**
- Create: `audio/events.py`
- Test: `tests/test_events.py`

- [ ] **Step 1: Write failing tests for event payloads**

```python
# tests/test_events.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/wanlizhou/Learn/Projects/radio-podcast && python -m pytest tests/test_events.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement event types**

```python
# audio/events.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/wanlizhou/Learn/Projects/radio-podcast && python -m pytest tests/test_events.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add audio/events.py tests/test_events.py
git commit -m "feat(audio): SSE event payload types"
```

---

### Task 3: SegmentResult type + TTSClient

**Files:**
- Create: `audio/tts.py`
- Test: `tests/test_tts.py`

This is the core of the audio component. TTSClient wraps ElevenLabs SDK batch TTS, applies pronunciation rules, writes MP3 to disk atomically, parses duration via mutagen, and returns a `SegmentResult`.

- [ ] **Step 1: Write failing tests for TTSClient**

```python
# tests/test_tts.py
"""Tests for TTSClient batch TTS synthesis.

Spec: audio/docs/DESIGN.md §Interface contract, §Batch path
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from audio.tts import TTSClient, SegmentResult


# Minimal valid MP3 frame (MPEG1 Layer3, 128kbps, 44100Hz, ~26ms)
# This is a real MP3 sync word + header so mutagen can parse it.
FAKE_MP3_HEADER = (
    b"\xff\xfb\x90\x00"  # sync + MPEG1, Layer3, 128kbps, 44100Hz
    + b"\x00" * 413       # pad to one full frame (417 bytes for this config)
)


@pytest.fixture
def tmp_output_dir(tmp_path: Path) -> str:
    return str(tmp_path / "episodes")


@pytest.fixture
def mock_elevenlabs():
    """Patch the ElevenLabs client to return fake MP3 bytes."""
    with patch("audio.tts.ElevenLabs") as MockClient:
        instance = MockClient.return_value
        # SDK convert() returns an iterator of bytes
        instance.text_to_speech.convert.return_value = iter([FAKE_MP3_HEADER])
        yield instance


class TestTTSClientInit:
    def test_creates_with_defaults(self, tmp_output_dir):
        client = TTSClient(api_key="sk_test", output_dir=tmp_output_dir)
        assert client._max_concurrent == 4

    def test_custom_max_concurrent(self, tmp_output_dir):
        client = TTSClient(api_key="sk_test", output_dir=tmp_output_dir, max_concurrent=2)
        assert client._max_concurrent == 2


class TestTTSClientSynthesize:
    @pytest.mark.asyncio
    async def test_writes_mp3_to_disk(self, tmp_output_dir, mock_elevenlabs):
        client = TTSClient(api_key="sk_test", output_dir=tmp_output_dir)
        result = await client.synthesize(
            text="Hello world",
            voice_id="voice123",
            episode_id="ep1",
            segment_index=0,
        )
        mp3_path = Path(tmp_output_dir) / "ep1" / "segment_0.mp3"
        assert mp3_path.exists()
        assert mp3_path.stat().st_size > 0

    @pytest.mark.asyncio
    async def test_returns_segment_result_shape(self, tmp_output_dir, mock_elevenlabs):
        client = TTSClient(api_key="sk_test", output_dir=tmp_output_dir)
        result = await client.synthesize(
            text="Hello world",
            voice_id="voice123",
            episode_id="ep1",
            segment_index=0,
        )
        assert result["segment_index"] == 0
        assert result["url"] == "/audio/ep1/segment_0.mp3"
        assert isinstance(result["duration_ms"], int)
        assert isinstance(result["generation_time_ms"], int)
        assert result["character_count"] == len("Hello world")
        assert result["billed_character_count"] == len("Hello world")

    @pytest.mark.asyncio
    async def test_applies_pronunciation_rules(self, tmp_output_dir, mock_elevenlabs):
        client = TTSClient(api_key="sk_test", output_dir=tmp_output_dir)
        await client.synthesize(
            text="Follow @ofmiles on AI",
            voice_id="voice123",
            episode_id="ep1",
            segment_index=0,
        )
        # Verify the text sent to ElevenLabs had pronunciation applied
        call_kwargs = mock_elevenlabs.text_to_speech.convert.call_args
        sent_text = call_kwargs.kwargs.get("text") or call_kwargs[1].get("text")
        assert "@ofmiles" not in sent_text
        assert "ofmiles" in sent_text
        assert "A I" in sent_text

    @pytest.mark.asyncio
    async def test_uses_configured_model_and_format(self, tmp_output_dir, mock_elevenlabs):
        client = TTSClient(api_key="sk_test", output_dir=tmp_output_dir)
        await client.synthesize(
            text="Test",
            voice_id="voice123",
            episode_id="ep1",
            segment_index=0,
        )
        call_kwargs = mock_elevenlabs.text_to_speech.convert.call_args
        assert call_kwargs.kwargs["model_id"] == "eleven_turbo_v2_5"
        assert call_kwargs.kwargs["output_format"] == "mp3_44100_128"

    @pytest.mark.asyncio
    async def test_fallback_duration_estimate_on_mutagen_failure(
        self, tmp_output_dir, mock_elevenlabs
    ):
        """When mutagen can't parse duration, estimate from char count."""
        # Return garbage bytes that mutagen can't parse
        mock_elevenlabs.text_to_speech.convert.return_value = iter([b"\x00" * 100])
        client = TTSClient(api_key="sk_test", output_dir=tmp_output_dir)
        result = await client.synthesize(
            text="A" * 150,  # 150 chars → ~1 sec at 150 chars/sec → 1000ms
            voice_id="voice123",
            episode_id="ep1",
            segment_index=0,
        )
        assert result["duration_estimated"] is True
        assert result["duration_ms"] == 1000  # 150 chars / 150 chars_per_sec * 1000

    @pytest.mark.asyncio
    async def test_concurrent_semaphore_respected(self, tmp_output_dir, mock_elevenlabs):
        """Verify max_concurrent limits parallel calls."""
        client = TTSClient(api_key="sk_test", output_dir=tmp_output_dir, max_concurrent=2)
        assert client._semaphore._value == 2
```

- [ ] **Step 2: Install pytest-asyncio**

Run: `cd /Users/wanlizhou/Learn/Projects/radio-podcast && source venv/bin/activate && pip install pytest-asyncio`

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /Users/wanlizhou/Learn/Projects/radio-podcast && python -m pytest tests/test_tts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'audio.tts'`

- [ ] **Step 4: Implement TTSClient**

```python
# audio/tts.py
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/wanlizhou/Learn/Projects/radio-podcast && python -m pytest tests/test_tts.py -v`
Expected: All 7 tests PASS

- [ ] **Step 6: Commit**

```bash
git add audio/tts.py tests/test_tts.py
git commit -m "feat(audio): TTSClient with batch synthesis, pronunciation, and duration parsing"
```

---

### Task 4: Orchestrator — parallel dispatch + error handling

**Files:**
- Create: `audio/orchestrator.py`
- Test: `tests/test_orchestrator.py`

The orchestrator fires segment 1 first (critical path), then segments 2-N in parallel. It collects results and handles per-segment errors per the error handling matrix.

- [ ] **Step 1: Write failing tests for the orchestrator**

```python
# tests/test_orchestrator.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/wanlizhou/Learn/Projects/radio-podcast && python -m pytest tests/test_orchestrator.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement orchestrator**

```python
# audio/orchestrator.py
"""Audio orchestrator — parallel TTS dispatch with error handling.

Fires segment 1 first (critical path), then segments 2-N in parallel.
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/wanlizhou/Learn/Projects/radio-podcast && python -m pytest tests/test_orchestrator.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add audio/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(audio): orchestrator with parallel dispatch and error handling"
```

---

### Task 5: Offline concat (ffmpeg)

**Files:**
- Create: `audio/concat.py`
- Test: `tests/test_concat.py`

NOT on the live-gen path. Assembles single-file MP3 post-rehearsal.

- [ ] **Step 1: Write failing tests for concat**

```python
# tests/test_concat.py
"""Tests for offline ffmpeg concat.

Spec: audio/docs/DESIGN.md §Offline Concat
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from audio.concat import concat_episode, build_concat_list


class TestBuildConcatList:
    def test_sorts_numerically(self, tmp_path: Path):
        """segment_2 comes before segment_10 (numeric, not lexicographic)."""
        episode_dir = tmp_path / "ep1"
        episode_dir.mkdir()
        for i in [0, 2, 10, 1]:
            (episode_dir / f"segment_{i}.mp3").write_bytes(b"\xff" * 10)

        lines = build_concat_list(episode_dir)
        # Should be sorted: 0, 1, 2, 10
        assert len(lines) == 4
        assert "segment_0.mp3" in lines[0]
        assert "segment_1.mp3" in lines[1]
        assert "segment_2.mp3" in lines[2]
        assert "segment_10.mp3" in lines[3]

    def test_empty_dir_returns_empty(self, tmp_path: Path):
        episode_dir = tmp_path / "ep_empty"
        episode_dir.mkdir()
        assert build_concat_list(episode_dir) == []


class TestConcatEpisode:
    @patch("audio.concat.subprocess.run")
    def test_calls_ffmpeg_with_correct_args(
        self, mock_run: MagicMock, tmp_path: Path
    ):
        mock_run.return_value = MagicMock(returncode=0)
        episode_dir = tmp_path / "ep1"
        episode_dir.mkdir()
        (episode_dir / "segment_0.mp3").write_bytes(b"\xff" * 10)
        (episode_dir / "segment_1.mp3").write_bytes(b"\xff" * 10)
        exports_dir = tmp_path / "exports"

        output = concat_episode(
            episode_dir=episode_dir,
            episode_id="ep1",
            exports_dir=exports_dir,
        )
        assert mock_run.called
        args = mock_run.call_args[0][0]
        assert args[0] == "ffmpeg"
        assert "-c" in args and "copy" in args
        assert output == exports_dir / "episode-ep1.mp3"

    @patch("audio.concat.subprocess.run")
    def test_creates_exports_dir(self, mock_run: MagicMock, tmp_path: Path):
        mock_run.return_value = MagicMock(returncode=0)
        episode_dir = tmp_path / "ep1"
        episode_dir.mkdir()
        (episode_dir / "segment_0.mp3").write_bytes(b"\xff" * 10)
        exports_dir = tmp_path / "new_exports"

        concat_episode(
            episode_dir=episode_dir,
            episode_id="ep1",
            exports_dir=exports_dir,
        )
        assert exports_dir.exists()

    @patch("audio.concat.subprocess.run")
    def test_raises_on_ffmpeg_failure(self, mock_run: MagicMock, tmp_path: Path):
        mock_run.side_effect = subprocess.CalledProcessError(1, "ffmpeg")
        episode_dir = tmp_path / "ep1"
        episode_dir.mkdir()
        (episode_dir / "segment_0.mp3").write_bytes(b"\xff" * 10)

        with pytest.raises(subprocess.CalledProcessError):
            concat_episode(
                episode_dir=episode_dir,
                episode_id="ep1",
                exports_dir=tmp_path / "exports",
            )

    def test_raises_on_no_segments(self, tmp_path: Path):
        episode_dir = tmp_path / "ep_empty"
        episode_dir.mkdir()
        with pytest.raises(ValueError, match="No segment files"):
            concat_episode(
                episode_dir=episode_dir,
                episode_id="ep_empty",
                exports_dir=tmp_path / "exports",
            )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/wanlizhou/Learn/Projects/radio-podcast && python -m pytest tests/test_concat.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement concat**

```python
# audio/concat.py
"""Offline ffmpeg concat — single-file Episode MP3 for handoff.

NOT on the live-gen path. Runs post-rehearsal to produce a single MP3
for Slack/Drive handoff to judges.

Spec: audio/docs/DESIGN.md §Offline Concat
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path


def build_concat_list(episode_dir: Path) -> list[str]:
    """Build ffmpeg concat-demuxer file lines, sorted numerically.

    Returns list of "file '<path>'" lines. macOS-safe (no ls -v).
    """
    files = sorted(
        episode_dir.glob("segment_*.mp3"),
        key=lambda f: int(re.search(r"segment_(\d+)", f.name).group(1)),
    )
    return [f"file '{f}'" for f in files]


def concat_episode(
    episode_dir: Path,
    episode_id: str,
    exports_dir: Path = Path("./exports"),
) -> Path:
    """Concatenate per-segment MP3s into a single episode file.

    Uses ffmpeg concat demuxer with -c copy (no re-encoding).

    Args:
        episode_dir: directory containing segment_N.mp3 files.
        episode_id: used for the output filename.
        exports_dir: output directory (created if absent).

    Returns:
        Path to the exported single-file MP3.

    Raises:
        ValueError: if no segment files found.
        subprocess.CalledProcessError: if ffmpeg fails.
    """
    lines = build_concat_list(episode_dir)
    if not lines:
        raise ValueError(f"No segment files found in {episode_dir}")

    exports_dir.mkdir(parents=True, exist_ok=True)
    output_path = exports_dir / f"episode-{episode_id}.mp3"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=True
    ) as concat_file:
        concat_file.write("\n".join(lines))
        concat_file.flush()

        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_file.name,
                "-c", "copy",
                str(output_path),
            ],
            check=True,
            capture_output=True,
        )

    return output_path
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/wanlizhou/Learn/Projects/radio-podcast && python -m pytest tests/test_concat.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add audio/concat.py tests/test_concat.py
git commit -m "feat(audio): offline ffmpeg concat for single-file episode export"
```

---

### Task 6: Gitignore + package exports

**Files:**
- Modify: `.gitignore`
- Modify: `audio/__init__.py`

- [ ] **Step 1: Add data/episodes and exports to .gitignore**

Append to `.gitignore`:
```
data/
exports/
```

- [ ] **Step 2: Populate `audio/__init__.py` with public API re-exports**

```python
# audio/__init__.py
"""Audio component — TTS generation, parallel dispatch, offline concat.

Spec: audio/docs/DESIGN.md
"""

from audio.tts import TTSClient, SegmentResult
from audio.orchestrator import generate_episode_audio, AudioResult
from audio.pronunciation import apply_pronunciation
from audio.concat import concat_episode
from audio.events import SegmentDone, SegmentDelayed, EpisodeDone, EpisodeFailed

__all__ = [
    "TTSClient",
    "SegmentResult",
    "generate_episode_audio",
    "AudioResult",
    "apply_pronunciation",
    "concat_episode",
    "SegmentDone",
    "SegmentDelayed",
    "EpisodeDone",
    "EpisodeFailed",
]
```

- [ ] **Step 3: Run full test suite**

Run: `cd /Users/wanlizhou/Learn/Projects/radio-podcast && python -m pytest tests/ -v`
Expected: All tests PASS (pronunciation, events, tts, orchestrator, concat, plus existing extractor/guardrails/segments tests)

- [ ] **Step 4: Commit**

```bash
git add .gitignore audio/__init__.py
git commit -m "chore(audio): gitignore data/exports dirs, populate package exports"
```

---

## Self-Review

**Spec coverage check:**

| DESIGN.md section | Task |
|---|---|
| TTS generation (batch API) | Task 3 (TTSClient) |
| Parallel batch pipeline (seg 1 first, 2-N parallel) | Task 4 (orchestrator) |
| Music beds | Out of scope — browser-side TypeScript, player component not finalized |
| Offline concat (ffmpeg) | Task 5 |
| SSE Event Contracts | Task 2 (event types) |
| ElevenLabs Integration (SDK) | Task 3 |
| SSML / Pronunciation handling | Task 1 |
| Interface contract (TTSClient, SegmentResult) | Task 3 |
| Voice mapping | Task 4 (orchestrator uses VOICE_MAP) |
| Parallel dispatch | Task 4 |
| MusicFiller (browser-side TS) | Out of scope — player component not finalized |
| Error Handling Matrix | Task 4 (per-segment skip, pipeline timeout) |
| Voices | Already done — `audio/config.py` locked after probe |
| Music beds (download assets) | Out of scope — manual Day 0/1 task, not code |
| Gapless Transition Strategy | Out of scope — player-side concern |
| Offline Concat | Task 5 |

**Out-of-scope items (explicitly per DESIGN.md):**
- `MusicFiller` — browser-side TypeScript, owned by `player` component (not finalized)
- `SegmentDelayed` SSE emission — player-side detection, defined as type only
- Music bed download — manual task, not code
- api-storage SSE wire format — not finalized
- Player — not finalized

**Placeholder scan:** None found. All steps have complete code.

**Type consistency check:** `SegmentResult` TypedDict shape is consistent across tts.py and orchestrator.py tests. `SegmentDone` event references match. `VOICE_MAP` import is consistent. `AudioResult` fields match test assertions.

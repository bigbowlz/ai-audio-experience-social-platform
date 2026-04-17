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

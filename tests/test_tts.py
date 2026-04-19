"""Tests for TTSClient batch TTS synthesis.

Spec: audio/docs/DESIGN.md §Interface contract, §Batch path, §Error Handling Matrix
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from audio.tts import TTSClient, SegmentResult, _RETRYABLE_STATUS_CODES


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
        # audio_path is the on-disk path the v0 CLI passes to afplay.
        # Regression guard for "AudioFileOpen failed ('wht?')" bug where
        # the web-route url was being handed to afplay verbatim.
        expected_mp3 = Path(tmp_output_dir) / "ep1" / "segment_0.mp3"
        assert result["audio_path"] == str(expected_mp3)
        assert Path(result["audio_path"]).exists()
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
            text="A" * 150,  # 150 chars → ~11.5 sec at 13 chars/sec → 11538ms
            voice_id="voice123",
            episode_id="ep1",
            segment_index=0,
        )
        assert result["duration_estimated"] is True
        assert result["duration_ms"] == 11538  # 150 chars / 13 chars_per_sec * 1000

    @pytest.mark.asyncio
    async def test_concurrent_semaphore_respected(self, tmp_output_dir, mock_elevenlabs):
        """Verify max_concurrent limits parallel calls."""
        client = TTSClient(api_key="sk_test", output_dir=tmp_output_dir, max_concurrent=2)
        assert client._semaphore._value == 2

    @pytest.mark.asyncio
    async def test_uses_voice_settings_from_config(self, tmp_output_dir, mock_elevenlabs):
        """Voice settings should come from config, not hardcoded."""
        client = TTSClient(api_key="sk_test", output_dir=tmp_output_dir)
        await client.synthesize(
            text="Test", voice_id="v1", episode_id="ep1", segment_index=0,
        )
        call_kwargs = mock_elevenlabs.text_to_speech.convert.call_args
        settings = call_kwargs.kwargs["voice_settings"]
        assert settings["stability"] == 0.5
        assert settings["use_speaker_boost"] is True


class TestTTSRetryLogic:
    """Spec: audio/docs/DESIGN.md §Error Handling Matrix"""

    @pytest.mark.asyncio
    async def test_retries_on_retryable_error(self, tmp_output_dir, mock_elevenlabs):
        """429/5xx triggers exponential backoff retry up to MAX_RETRIES."""
        api_error = Exception("rate limited")
        api_error.status_code = 429  # type: ignore[attr-defined]

        call_count = 0
        def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise api_error
            return iter([FAKE_MP3_HEADER])

        mock_elevenlabs.text_to_speech.convert.side_effect = side_effect

        with patch("audio.tts.asyncio.sleep"):  # skip actual backoff delays
            client = TTSClient(api_key="sk_test", output_dir=tmp_output_dir)
            result = await client.synthesize(
                text="Retry me", voice_id="v1", episode_id="ep1", segment_index=0,
            )
        assert result["segment_index"] == 0
        assert call_count == 3  # 2 failures + 1 success

    @pytest.mark.asyncio
    async def test_billed_chars_accumulate_across_retries(
        self, tmp_output_dir, mock_elevenlabs
    ):
        """ElevenLabs bills per-attempt, so billed_character_count must accumulate."""
        api_error = Exception("server error")
        api_error.status_code = 500  # type: ignore[attr-defined]

        call_count = 0
        def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise api_error
            return iter([FAKE_MP3_HEADER])

        mock_elevenlabs.text_to_speech.convert.side_effect = side_effect

        with patch("audio.tts.asyncio.sleep"):
            client = TTSClient(api_key="sk_test", output_dir=tmp_output_dir)
            result = await client.synthesize(
                text="Bill me", voice_id="v1", episode_id="ep1", segment_index=0,
            )
        char_count = result["character_count"]
        # 2 attempts (1 failed + 1 success) = 2x billed
        assert result["billed_character_count"] == char_count * 2

    @pytest.mark.asyncio
    async def test_unrecoverable_error_raises_immediately(
        self, tmp_output_dir, mock_elevenlabs
    ):
        """401/422 should NOT retry — raise immediately."""
        api_error = Exception("unauthorized")
        api_error.status_code = 401  # type: ignore[attr-defined]
        mock_elevenlabs.text_to_speech.convert.side_effect = api_error

        client = TTSClient(api_key="sk_test", output_dir=tmp_output_dir)
        with pytest.raises(Exception, match="unauthorized"):
            await client.synthesize(
                text="Bad key", voice_id="v1", episode_id="ep1", segment_index=0,
            )
        # Should have been called exactly once (no retry)
        assert mock_elevenlabs.text_to_speech.convert.call_count == 1

    @pytest.mark.asyncio
    async def test_exhausted_retries_raises(self, tmp_output_dir, mock_elevenlabs):
        """When all MAX_RETRIES attempts fail, the final error propagates."""
        api_error = Exception("server error")
        api_error.status_code = 500  # type: ignore[attr-defined]
        mock_elevenlabs.text_to_speech.convert.side_effect = api_error

        from audio.config import MAX_RETRIES

        with patch("audio.tts.asyncio.sleep"):
            client = TTSClient(api_key="sk_test", output_dir=tmp_output_dir)
            with pytest.raises(Exception, match="server error"):
                await client.synthesize(
                    text="Fail forever", voice_id="v1",
                    episode_id="ep1", segment_index=0,
                )
        assert mock_elevenlabs.text_to_speech.convert.call_count == 1 + MAX_RETRIES

    @pytest.mark.asyncio
    async def test_timeout_retries_once_then_raises(
        self, tmp_output_dir, mock_elevenlabs
    ):
        """Timeout retries once (attempt 0), then raises on attempt 1."""
        with patch("audio.tts.asyncio.wait_for", side_effect=TimeoutError("timeout")):
            client = TTSClient(api_key="sk_test", output_dir=tmp_output_dir)
            with pytest.raises(TimeoutError):
                await client.synthesize(
                    text="Slow", voice_id="v1",
                    episode_id="ep1", segment_index=0,
                )

    @pytest.mark.asyncio
    async def test_is_retryable_checks_status_codes(self, tmp_output_dir):
        client = TTSClient(api_key="sk_test", output_dir=tmp_output_dir)
        for code in [429, 500, 502, 503]:
            exc = Exception()
            exc.status_code = code  # type: ignore[attr-defined]
            assert client._is_retryable(exc) is True

        for code in [401, 403, 422]:
            exc = Exception()
            exc.status_code = code  # type: ignore[attr-defined]
            assert client._is_retryable(exc) is False

    @pytest.mark.asyncio
    async def test_is_retryable_connection_errors(self, tmp_output_dir):
        """ConnectionError and OSError are retryable; ValueError is not."""
        client = TTSClient(api_key="sk_test", output_dir=tmp_output_dir)
        assert client._is_retryable(ConnectionError("reset")) is True
        assert client._is_retryable(OSError("network unreachable")) is True
        assert client._is_retryable(ValueError("bad input")) is False

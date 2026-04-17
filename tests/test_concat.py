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

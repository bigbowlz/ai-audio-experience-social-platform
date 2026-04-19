"""Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Addendum Task 2S.4"""
from __future__ import annotations

from unittest import mock

import pytest

from storage.export import concat_episode_mp3


def _setup_segments(tmp_path, monkeypatch, episode_id="ep-xyz", n=3):
    ep_dir = tmp_path / "episodes" / episode_id
    ep_dir.mkdir(parents=True)
    for i in range(n):
        (ep_dir / f"segment_{i}.mp3").write_bytes(b"fake mp3 bytes")
    monkeypatch.setattr("storage.episode_dir._EPISODES_DIR", tmp_path / "episodes")
    monkeypatch.setattr("storage.export._EXPORTS_DIR", tmp_path / "exports")
    return ep_dir


def test_concat_writes_to_exports_dir(tmp_path, monkeypatch):
    _setup_segments(tmp_path, monkeypatch)

    # Stub ffmpeg: pretend it succeeded and wrote the output.
    out_path_holder = {}

    def fake_run(args, capture_output, text):
        # Output path is last positional arg.
        out = args[-1]
        out_path_holder["out"] = out
        # Simulate ffmpeg actually creating the file.
        from pathlib import Path as _P
        _P(out).parent.mkdir(parents=True, exist_ok=True)
        _P(out).write_bytes(b"concat result")
        return mock.Mock(returncode=0, stderr="")

    monkeypatch.setattr("storage.export.shutil.which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr("storage.export.subprocess.run", fake_run)

    result = concat_episode_mp3("ep-xyz")
    assert result.exists()
    assert result.name == "episode-ep-xyz.mp3"
    assert result.parent.name == "exports"


def test_concat_raises_when_no_segments(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.episode_dir._EPISODES_DIR", tmp_path / "episodes")
    monkeypatch.setattr("storage.export._EXPORTS_DIR", tmp_path / "exports")
    (tmp_path / "episodes" / "ep-empty").mkdir(parents=True)

    with pytest.raises(RuntimeError, match="no segment_"):
        concat_episode_mp3("ep-empty")


def test_concat_raises_when_ffmpeg_missing(tmp_path, monkeypatch):
    _setup_segments(tmp_path, monkeypatch)
    monkeypatch.setattr("storage.export.shutil.which", lambda _: None)

    with pytest.raises(RuntimeError, match="ffmpeg not found"):
        concat_episode_mp3("ep-xyz")


def test_concat_raises_when_ffmpeg_returns_nonzero(tmp_path, monkeypatch):
    _setup_segments(tmp_path, monkeypatch)
    monkeypatch.setattr("storage.export.shutil.which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "storage.export.subprocess.run",
        lambda *a, **k: mock.Mock(returncode=1, stderr="ffmpeg: invalid syntax"),
    )
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        concat_episode_mp3("ep-xyz")

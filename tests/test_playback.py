"""AfplaySession subprocess lifecycle tests.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Task 2.1
"""
from __future__ import annotations

import signal
from unittest import mock

import pytest

from player.playback import AfplaySession


def _fake_popen(rc_on_wait: int = 0) -> mock.Mock:
    proc = mock.Mock()
    proc.pid = 4242
    proc.returncode = None

    def _wait(timeout=None):
        proc.returncode = rc_on_wait
        return rc_on_wait

    proc.wait.side_effect = _wait
    proc.send_signal = mock.Mock()
    proc.terminate = mock.Mock()
    return proc


def test_start_spawns_afplay_with_path(monkeypatch):
    fake_proc = _fake_popen()
    popen = mock.Mock(return_value=fake_proc)
    monkeypatch.setattr("player.playback.subprocess.Popen", popen)

    session = AfplaySession("/tmp/seg0.mp3")
    session.start()

    popen.assert_called_once()
    args, _ = popen.call_args
    assert args[0] == ["/usr/bin/afplay", "/tmp/seg0.mp3"]


def test_pause_sends_sigstop_then_resume_sends_sigcont(monkeypatch):
    fake_proc = _fake_popen()
    monkeypatch.setattr(
        "player.playback.subprocess.Popen",
        mock.Mock(return_value=fake_proc),
    )

    session = AfplaySession("/tmp/seg0.mp3")
    session.start()
    session.pause()
    fake_proc.send_signal.assert_called_with(signal.SIGSTOP)
    assert session.is_paused is True

    session.resume()
    fake_proc.send_signal.assert_called_with(signal.SIGCONT)
    assert session.is_paused is False


def test_stop_terminates(monkeypatch):
    fake_proc = _fake_popen()
    monkeypatch.setattr(
        "player.playback.subprocess.Popen",
        mock.Mock(return_value=fake_proc),
    )

    session = AfplaySession("/tmp/seg0.mp3")
    session.start()
    session.stop()
    fake_proc.terminate.assert_called_once()


def test_wait_blocks_until_proc_exits(monkeypatch):
    fake_proc = _fake_popen(rc_on_wait=0)
    monkeypatch.setattr(
        "player.playback.subprocess.Popen",
        mock.Mock(return_value=fake_proc),
    )

    session = AfplaySession("/tmp/seg0.mp3")
    session.start()
    rc = session.wait()
    assert rc == 0
    fake_proc.wait.assert_called_once()


def test_start_raises_if_afplay_missing(monkeypatch):
    monkeypatch.setattr("player.playback.AFPLAY_PATH", "/nonexistent/afplay")
    monkeypatch.setattr("pathlib.Path.exists", lambda self: False)
    session = AfplaySession("/tmp/seg0.mp3")
    with pytest.raises(RuntimeError, match="afplay not found"):
        session.start()

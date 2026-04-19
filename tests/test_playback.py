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
    # stop() must also reap via wait() so `is_running` transitions to False
    # immediately. Task 2.3's q-keypress handler races this state check
    # against keypress events; a stale `is_running is True` would mask the
    # quit intent.
    fake_proc.wait.assert_called()
    assert session.is_running is False


def test_stop_while_paused_sends_sigcont_before_terminate(monkeypatch):
    """The hardest signal-ordering case: SIGSTOP'd process needs SIGCONT
    before SIGTERM can reach the handler. Without this, stop() would
    silently hang until the parent dies.
    """
    fake_proc = _fake_popen()
    monkeypatch.setattr(
        "player.playback.subprocess.Popen",
        mock.Mock(return_value=fake_proc),
    )

    session = AfplaySession("/tmp/seg0.mp3")
    session.start()
    session.pause()
    session.stop()

    # Signal sequence observed on send_signal: SIGSTOP (from pause),
    # then SIGCONT (from stop's unpause-before-terminate guard).
    signal_calls = [c.args[0] for c in fake_proc.send_signal.call_args_list]
    assert signal_calls == [signal.SIGSTOP, signal.SIGCONT]
    fake_proc.terminate.assert_called_once()
    assert session.is_paused is False
    assert session.is_running is False


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

"""Hotkey reader tests — fake stdin, verify termios raw mode is entered/exited.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Task 2.2
"""
from __future__ import annotations

from unittest import mock

from player.hotkeys import raw_key_reader, decode_key, KeyPress


def test_decode_single_chars():
    assert decode_key("l") == KeyPress.LIKE
    assert decode_key("s") == KeyPress.SKIP
    assert decode_key("r") == KeyPress.REPEAT
    assert decode_key("p") == KeyPress.PAUSE
    assert decode_key(" ") == KeyPress.PAUSE        # space also = pause
    assert decode_key("q") == KeyPress.QUIT
    assert decode_key("L") == KeyPress.LIKE         # case-insensitive
    assert decode_key("x") == KeyPress.UNKNOWN


def test_raw_key_reader_yields_and_restores_termios(monkeypatch):
    # Fake stdin: returns 'l', 's', 'q' then EOF ('').
    # Mock (not io.StringIO) — we need a working .fileno() too.
    fake_stdin = mock.Mock()
    fake_stdin.read.side_effect = ["l", "s", "q", ""]
    fake_stdin.fileno.return_value = 0
    monkeypatch.setattr("sys.stdin", fake_stdin)
    # Skip real termios calls in tests — verify via call recording.
    tcgetattr = mock.Mock(return_value=["saved"])
    tcsetattr = mock.Mock()
    setraw = mock.Mock()
    monkeypatch.setattr("player.hotkeys.termios.tcgetattr", tcgetattr)
    monkeypatch.setattr("player.hotkeys.termios.tcsetattr", tcsetattr)
    monkeypatch.setattr("player.hotkeys.tty.setraw", setraw)

    collected: list[KeyPress] = []
    with raw_key_reader() as keys:
        for key in keys:
            collected.append(key)
            if key == KeyPress.QUIT:
                break

    assert collected == [KeyPress.LIKE, KeyPress.SKIP, KeyPress.QUIT]
    tcgetattr.assert_called_once()
    setraw.assert_called_once()
    tcsetattr.assert_called_once_with(mock.ANY, mock.ANY, ["saved"])

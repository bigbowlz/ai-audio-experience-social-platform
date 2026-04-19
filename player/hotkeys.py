"""termios raw-mode single-key reader for the CLI player.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Task 2.2
"""
from __future__ import annotations

import enum
import sys
import termios
import tty
from collections.abc import Iterator
from contextlib import contextmanager


class KeyPress(str, enum.Enum):
    LIKE = "like"
    SKIP = "skip"
    REPEAT = "repeat"
    PAUSE = "pause"
    QUIT = "quit"
    UNKNOWN = "unknown"


_KEY_MAP: dict[str, KeyPress] = {
    "l": KeyPress.LIKE,
    "s": KeyPress.SKIP,
    "r": KeyPress.REPEAT,
    "p": KeyPress.PAUSE,
    " ": KeyPress.PAUSE,
    "q": KeyPress.QUIT,
}


def decode_key(char: str) -> KeyPress:
    """Pure function: char → KeyPress. Case-insensitive on letters."""
    return _KEY_MAP.get(char.lower(), KeyPress.UNKNOWN)


@contextmanager
def raw_key_reader() -> Iterator[Iterator[KeyPress]]:
    """Context manager that yields an iterator of KeyPress values from stdin.

    Enters termios raw mode on __enter__, restores on __exit__ (even on
    exception). Inner iterator reads one char at a time via sys.stdin.read(1)
    and decodes via _KEY_MAP.

    Requires a real TTY on stdin. If stdin is piped/redirected, the
    `termios.tcgetattr()` call raises `termios.error: (25, 'Inappropriate
    ioctl for device')` before the try block. The v0 CLI player is
    interactive by design, so this is a caller-level precondition, not
    a recoverable error — Task 2.3 does not attempt to recover.
    """
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        def _gen() -> Iterator[KeyPress]:
            while True:
                ch = sys.stdin.read(1)
                if not ch:
                    return
                yield decode_key(ch)
        yield _gen()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)

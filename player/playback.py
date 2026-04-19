"""afplay subprocess wrapper — sync, macOS-only.

Used from async code via asyncio.to_thread to avoid blocking the event loop.
SIGSTOP / SIGCONT give us real pause/resume (afplay has no built-in pause).

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Task 2.1
"""
from __future__ import annotations

import signal
import subprocess
from pathlib import Path

AFPLAY_PATH = "/usr/bin/afplay"


class AfplaySession:
    """One playback session for one audio file.

    Not thread-safe — caller owns sequencing. Multiple sessions can exist
    at once, but the CLI player always runs exactly one at a time.
    """

    def __init__(self, audio_path: str | Path) -> None:
        self._path = str(audio_path)
        self._proc: subprocess.Popen | None = None
        self._paused = False

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def start(self) -> None:
        if self.is_running:
            # Double-start would leak the first subprocess without reaping it.
            # Caller bug — fail loud rather than silently orphan a process.
            raise RuntimeError("start() called on already-running session")
        if not Path(AFPLAY_PATH).exists():
            raise RuntimeError(
                f"afplay not found at {AFPLAY_PATH}. v0 CLI playback is "
                "macOS-only; see docs/specs/2026-04-18-v0-cli-pivot-plan.md "
                "§Non-goals #2."
            )
        self._proc = subprocess.Popen([AFPLAY_PATH, self._path])
        self._paused = False

    def pause(self) -> None:
        if self._proc is None or self._paused:
            return
        self._proc.send_signal(signal.SIGSTOP)
        self._paused = True

    def resume(self) -> None:
        if self._proc is None or not self._paused:
            return
        self._proc.send_signal(signal.SIGCONT)
        self._paused = False

    def stop(self) -> None:
        if self._proc is None:
            return
        # If paused, SIGCONT first so SIGTERM can actually reap the process.
        if self._paused:
            self._proc.send_signal(signal.SIGCONT)
            self._paused = False
        self._proc.terminate()
        # Reap the process so `returncode` is populated; otherwise
        # `is_running` would continue to report True until the OS delivers
        # SIGTERM + the parent reaps — a ~10ms window where the async
        # caller (Task 2.3's q-keypress handler) could race on stale state.
        self._proc.wait()

    def wait(self, timeout: float | None = None) -> int:
        if self._proc is None:
            raise RuntimeError("wait() called before start()")
        return self._proc.wait(timeout=timeout)

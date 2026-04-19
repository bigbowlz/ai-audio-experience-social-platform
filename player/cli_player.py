"""CLI player — plays segments sequentially, dispatches hotkeys, emits feedback.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Task 2.3
"""
from __future__ import annotations

import asyncio
import os
import sys
import termios
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from player.hotkeys import KeyPress, raw_key_reader
from player.playback import AfplaySession


@dataclass
class FeedbackSignal:
    user_id: str
    episode_id: str
    segment_index: int
    agent: str
    pitch_title: str
    signal: str  # "like" | "skip" | "replay"
    ts: str      # ISO 8601 UTC


async def _run_key_reader() -> AsyncIterator[KeyPress]:
    """Wrap the sync raw_key_reader in an async iterator via to_thread.

    Seam: tests monkeypatch this to a scripted async generator.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[KeyPress | None] = asyncio.Queue()

    def _pump() -> None:
        with raw_key_reader() as keys:
            for k in keys:
                loop.call_soon_threadsafe(queue.put_nowait, k)
                if k == KeyPress.QUIT:
                    return
        loop.call_soon_threadsafe(queue.put_nowait, None)

    task = asyncio.create_task(asyncio.to_thread(_pump))
    try:
        while True:
            k = await queue.get()
            if k is None:
                return
            yield k
            if k == KeyPress.QUIT:
                return
    finally:
        if not task.done():
            task.cancel()


def _print_seg(seg: dict, playing: bool, overwrite: bool = False) -> None:
    """Print (or overwrite) the segment status line.

    Uses \\r\\n so output is correct while tty.setraw has OPOST disabled.
    overwrite=True moves the cursor up one line before reprinting (for
    toggling pause/resume on the same visual row).
    """
    icon = "\u25b6" if playing else "\u23f8"
    hints = (
        "(l=like  s=skip  r=repeat  p=pause  q=quit)"
        if playing
        else "(p=play  l=like  s=skip  r=repeat  q=quit)"
    )
    line = (
        f"  {icon} [segment {seg['segment_index']}] "
        f"{seg['agent']}: {seg['pitch_title']}  {hints}"
    )
    if overwrite:
        sys.stdout.write(f"\033[1A\r{line}\033[K\r\n")
    else:
        sys.stdout.write(f"{line}\r\n")
    sys.stdout.flush()


async def play_episode(
    segments: list[dict],
    user_id: str,
    episode_id: str,
    on_feedback: Callable[[FeedbackSignal], Awaitable[None]],
) -> None:
    """Play each segment via afplay, listen for hotkeys, emit feedback.

    Semantics per hotkey reference table (spec §Phase 2).
    """
    # Save terminal settings before the pump thread enters raw mode.
    # Restored in finally so the shell prompt is never left in raw mode
    # when play_episode exits by any path: natural end, skip-to-end, or quit.
    fd = sys.stdin.fileno()
    try:
        _saved_tty = termios.tcgetattr(fd)
    except termios.error:
        _saved_tty = None

    try:
        key_source = _run_key_reader()
        key_task: asyncio.Task[KeyPress | None] | None = None
        quit_requested = False
        keys_exhausted = False  # True once key stream ends (not the same as quit)

        async def _next_key() -> KeyPress | None:
            nonlocal keys_exhausted
            if keys_exhausted:
                return None
            try:
                return await key_source.__anext__()
            except (StopAsyncIteration, StopIteration):
                keys_exhausted = True
                return None

        i = 0
        while i < len(segments) and not quit_requested:
            seg = segments[i]
            session = AfplaySession(seg["url"])
            session.start()
            if i == 0:
                session.pause()
            _print_seg(seg, playing=(i != 0))
            playback_task = asyncio.create_task(asyncio.to_thread(session.wait))

            restart = False
            advance = False

            while not (advance or restart or quit_requested):
                if key_task is None and not keys_exhausted:
                    key_task = asyncio.create_task(_next_key())

                if key_task is not None and not keys_exhausted:
                    done, _ = await asyncio.wait(
                        {playback_task, key_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                else:
                    # Key stream exhausted — just wait for playback to finish.
                    await playback_task
                    done = {playback_task}

                # Process key first: if both completed simultaneously, handle the
                # hotkey before declaring natural-end so signals are not dropped.
                if key_task in done:
                    key = key_task.result()
                    key_task = None

                    if key is None:
                        # Key stream ended naturally — let playback finish.
                        keys_exhausted = True
                        continue

                    if key == KeyPress.QUIT:
                        session.stop()
                        playback_task.cancel()
                        quit_requested = True
                        break

                    if key == KeyPress.PAUSE:
                        if session.is_paused:
                            session.resume()
                            _print_seg(seg, playing=True, overwrite=True)
                        else:
                            session.pause()
                            _print_seg(seg, playing=False, overwrite=True)
                        continue

                    if key == KeyPress.REPEAT:
                        await _emit(on_feedback, user_id, episode_id, seg, "replay")
                        session.stop()
                        playback_task.cancel()
                        restart = True
                        break

                    if key == KeyPress.SKIP:
                        await _emit(on_feedback, user_id, episode_id, seg, "skip")
                        session.stop()
                        playback_task.cancel()
                        advance = True
                        break

                    if key == KeyPress.LIKE:
                        await _emit(on_feedback, user_id, episode_id, seg, "like")
                        continue

                    # UNKNOWN: ignore, keep playing.
                    continue

                # Key was not in done (or key_task is None/exhausted) — check playback.
                if playback_task in done:
                    advance = True
                    # Leave key_task alive — cancelling it would throw CancelledError
                    # into key_source and close the generator, killing key handling
                    # for all subsequent segments (keys_exhausted would become True).
                    break

            if restart:
                continue  # same i; replay the same segment.
            if advance and not quit_requested:
                await asyncio.sleep(1)
            i += 1

        if not quit_requested:
            sys.stdout.write("\r\nPlayback complete.\r\n")
            sys.stdout.flush()
            # _pump is blocking on sys.stdin.read(1); asyncio.run() waits for
            # the executor thread during shutdown, so we must unblock it.
            # Injecting 'q' makes _pump read it, put KeyPress.QUIT on the
            # queue, and return — cleanly exiting the thread.
            try:
                os.write(sys.stdin.fileno(), b"q")
            except OSError:
                pass

        if key_task is not None and not key_task.done():
            key_task.cancel()
    finally:
        if _saved_tty is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, _saved_tty)
            except termios.error:
                pass


async def _emit(
    on_feedback: Callable[[FeedbackSignal], Awaitable[None]],
    user_id: str,
    episode_id: str,
    seg: dict,
    signal: str,
) -> None:
    await on_feedback(FeedbackSignal(
        user_id=user_id,
        episode_id=episode_id,
        segment_index=seg["segment_index"],
        agent=seg["agent"],
        pitch_title=seg["pitch_title"],
        signal=signal,
        ts=datetime.now(timezone.utc).isoformat(),
    ))

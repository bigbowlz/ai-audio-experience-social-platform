"""CLI player integration tests — scripted hotkey sequences drive play_episode.

Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Task 2.3
"""
from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from player.cli_player import FeedbackSignal, play_episode
from player.hotkeys import KeyPress


@pytest.fixture
def fake_segments():
    """Three segments, minimal shape for the player."""
    return [
        {"segment_index": 0, "agent": "weather", "pitch_title": "Fog",    "url": "/tmp/0.mp3"},
        {"segment_index": 1, "agent": "calendar", "pitch_title": "Dentist", "url": "/tmp/1.mp3"},
        {"segment_index": 2, "agent": "youtube",  "pitch_title": "VTuber",  "url": "/tmp/2.mp3"},
    ]


def test_like_key_records_like_and_continues(fake_segments, monkeypatch):
    captured: list[FeedbackSignal] = []

    async def on_fb(sig: FeedbackSignal) -> None:
        captured.append(sig)

    # Stub AfplaySession to avoid spawning afplay.
    fake_session = mock.Mock()
    fake_session.start = mock.Mock()
    fake_session.wait = mock.Mock(return_value=0)
    fake_session.stop = mock.Mock()
    fake_session.pause = mock.Mock()
    fake_session.resume = mock.Mock()
    fake_session.is_paused = False
    monkeypatch.setattr(
        "player.cli_player.AfplaySession",
        mock.Mock(return_value=fake_session),
    )

    # Scripted keypresses: 'l' on segment 0, then segments 1 and 2 finish naturally.
    async def fake_key_source():
        yield KeyPress.LIKE

    monkeypatch.setattr("player.cli_player._run_key_reader", fake_key_source)

    asyncio.run(play_episode(
        fake_segments, user_id="dev", episode_id="ep-t",
        on_feedback=on_fb,
    ))

    assert len(captured) == 1
    assert captured[0].signal == "like"
    assert captured[0].agent == "weather"
    assert captured[0].segment_index == 0


def test_skip_key_terminates_current_and_advances(fake_segments, monkeypatch):
    captured: list[FeedbackSignal] = []

    async def on_fb(sig): captured.append(sig)

    fake_session = mock.Mock()
    fake_session.is_paused = False
    monkeypatch.setattr(
        "player.cli_player.AfplaySession",
        mock.Mock(return_value=fake_session),
    )

    async def fake_key_source():
        yield KeyPress.SKIP

    monkeypatch.setattr("player.cli_player._run_key_reader", fake_key_source)

    asyncio.run(play_episode(
        fake_segments, user_id="dev", episode_id="ep-t", on_feedback=on_fb,
    ))

    assert any(sig.signal == "skip" and sig.agent == "weather" for sig in captured)
    fake_session.stop.assert_called()


def test_pause_is_not_a_learning_signal(fake_segments, monkeypatch):
    captured: list[FeedbackSignal] = []

    async def on_fb(sig): captured.append(sig)

    fake_session = mock.Mock()
    # Pause toggles is_paused; simulate the session's behavior.
    _state = {"paused": False}
    def fake_pause():
        _state["paused"] = True
        fake_session.is_paused = True
    def fake_resume():
        _state["paused"] = False
        fake_session.is_paused = False
    fake_session.is_paused = False
    fake_session.pause.side_effect = fake_pause
    fake_session.resume.side_effect = fake_resume
    monkeypatch.setattr(
        "player.cli_player.AfplaySession",
        mock.Mock(return_value=fake_session),
    )

    async def fake_key_source():
        yield KeyPress.PAUSE
        yield KeyPress.PAUSE   # resume
        yield KeyPress.SKIP

    monkeypatch.setattr("player.cli_player._run_key_reader", fake_key_source)

    asyncio.run(play_episode(
        fake_segments, user_id="dev", episode_id="ep-t", on_feedback=on_fb,
    ))

    assert all(sig.signal != "pause" for sig in captured)
    assert any(sig.signal == "skip" for sig in captured)
    fake_session.pause.assert_called()
    fake_session.resume.assert_called()


def test_quit_stops_and_returns_immediately(fake_segments, monkeypatch):
    captured: list[FeedbackSignal] = []

    async def on_fb(sig): captured.append(sig)

    fake_session = mock.Mock()
    fake_session.is_paused = False
    monkeypatch.setattr(
        "player.cli_player.AfplaySession",
        mock.Mock(return_value=fake_session),
    )

    async def fake_key_source():
        yield KeyPress.QUIT

    monkeypatch.setattr("player.cli_player._run_key_reader", fake_key_source)

    asyncio.run(play_episode(
        fake_segments, user_id="dev", episode_id="ep-t", on_feedback=on_fb,
    ))

    assert captured == []
    fake_session.stop.assert_called()


def test_repeat_key_emits_replay_and_replays_same_segment(fake_segments, monkeypatch):
    """REPEAT is the only hotkey where segment index does NOT advance:
    restart=True + continue → same i re-enters the outer loop.
    This test pins the control-flow path that diverges from LIKE/SKIP/QUIT.
    """
    captured: list[FeedbackSignal] = []

    async def on_fb(sig): captured.append(sig)

    # Track how many times AfplaySession was instantiated — one per segment,
    # PLUS one more for the repeated segment.
    session_instances: list[mock.Mock] = []

    def new_session(url):
        s = mock.Mock()
        s.is_paused = False
        session_instances.append(s)
        return s

    monkeypatch.setattr("player.cli_player.AfplaySession", new_session)

    # Scripted keys: repeat on segment 0 ONCE, then segments 1 and 2 finish naturally.
    async def fake_key_source():
        yield KeyPress.REPEAT

    monkeypatch.setattr("player.cli_player._run_key_reader", fake_key_source)

    asyncio.run(play_episode(
        fake_segments, user_id="dev", episode_id="ep-t", on_feedback=on_fb,
    ))

    # Exactly one "replay" signal on segment 0 / weather agent.
    replay_sigs = [s for s in captured if s.signal == "replay"]
    assert len(replay_sigs) == 1
    assert replay_sigs[0].agent == "weather"
    assert replay_sigs[0].segment_index == 0

    # Segment 0 spawned two AfplaySessions (first plays + gets stopped; second
    # is the restart). Segments 1 and 2 spawn one each. Total = 4.
    assert len(session_instances) == 4
    # The first session got stopped (by the REPEAT handler).
    session_instances[0].stop.assert_called()

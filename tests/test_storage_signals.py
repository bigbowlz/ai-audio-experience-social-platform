"""Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Addendum Task 2S.2"""
from __future__ import annotations

import json

from storage.signals import append_signal, iter_signals


def _rec(user_id="dev", episode_id="ep-1", segment_index=0,
         agent="weather", pitch_title="Fog", signal="like",
         ts="2026-04-18T13:00:00+00:00"):
    return {
        "user_id": user_id, "episode_id": episode_id,
        "segment_index": segment_index, "agent": agent,
        "pitch_title": pitch_title, "signal": signal, "ts": ts,
    }


def test_append_and_iter_round_trip_single_episode(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.signals._SIGNALS_DIR", tmp_path / "signals")
    append_signal("dev", "ep-1", _rec(segment_index=0, agent="weather"))
    append_signal("dev", "ep-1", _rec(segment_index=1, agent="calendar", signal="skip"))

    recs = list(iter_signals("dev"))
    assert len(recs) == 2
    assert recs[0]["agent"] == "weather"
    assert recs[1]["signal"] == "skip"


def test_iter_signals_globs_across_episodes_for_user(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.signals._SIGNALS_DIR", tmp_path / "signals")
    append_signal("dev", "ep-1", _rec(episode_id="ep-1", agent="weather"))
    append_signal("dev", "ep-2", _rec(episode_id="ep-2", agent="youtube", signal="replay"))

    recs = list(iter_signals("dev"))
    # Ordered by filename sort (ep-1 before ep-2).
    assert [r["episode_id"] for r in recs] == ["ep-1", "ep-2"]


def test_iter_signals_isolates_users(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.signals._SIGNALS_DIR", tmp_path / "signals")
    append_signal("dev", "ep", _rec(user_id="dev"))
    append_signal("other", "ep", _rec(user_id="other"))

    assert len(list(iter_signals("dev"))) == 1
    assert list(iter_signals("dev"))[0]["user_id"] == "dev"


def test_iter_signals_skips_malformed_lines(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.signals._SIGNALS_DIR", tmp_path / "signals")
    user_dir = tmp_path / "signals" / "dev"
    user_dir.mkdir(parents=True)
    (user_dir / "ep-1.jsonl").write_text(
        json.dumps(_rec(signal="like")) + "\n"
        + "not json\n"
        + json.dumps(_rec(signal="skip")) + "\n"
    )
    recs = list(iter_signals("dev"))
    assert [r["signal"] for r in recs] == ["like", "skip"]


def test_iter_signals_empty_when_no_user_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.signals._SIGNALS_DIR", tmp_path / "signals")
    assert list(iter_signals("dev")) == []

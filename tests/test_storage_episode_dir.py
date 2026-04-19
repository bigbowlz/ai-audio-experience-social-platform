"""Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Addendum Task 2S.1"""
from __future__ import annotations

import uuid
from pathlib import Path

from storage.episode_dir import episode_dir, new_episode_id


def test_new_episode_id_is_uuid_format():
    eid = new_episode_id()
    assert len(eid) == 36
    assert eid.count("-") == 4
    # Pin v4 specifically — a regression to uuid.uuid1 (leaks MAC + time)
    # would pass the length/hyphen checks but fail this version assert.
    assert uuid.UUID(eid).version == 4


def test_new_episode_id_is_unique():
    assert new_episode_id() != new_episode_id()


def test_episode_dir_creates_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.episode_dir._EPISODES_DIR", tmp_path / "episodes")
    d1 = episode_dir("abc-123")
    assert d1.exists() and d1.is_dir()
    d2 = episode_dir("abc-123")
    assert d2 == d1  # idempotent


def test_episode_dir_returns_path_under_data_episodes(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.episode_dir._EPISODES_DIR", tmp_path / "episodes")
    d = episode_dir("xyz")
    assert d.name == "xyz"
    assert d.parent.name == "episodes"

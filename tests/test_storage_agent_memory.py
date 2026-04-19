"""Spec: docs/specs/2026-04-18-v0-cli-pivot-plan.md Addendum Task 2S.3"""
from __future__ import annotations

from storage.agent_memory import load_agent_memory, save_agent_memory


def test_load_returns_empty_dict_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "storage.agent_memory._AGENT_MEM_DIR", tmp_path / "agent_memory"
    )
    assert load_agent_memory("dev", "weather") == {}


def test_save_then_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "storage.agent_memory._AGENT_MEM_DIR", tmp_path / "agent_memory"
    )
    memory = {"topic_multiplier": {"cooking": 1.3}, "schema_version": 1}
    save_agent_memory("dev", "weather", memory)
    assert load_agent_memory("dev", "weather") == memory


def test_load_returns_empty_on_malformed_json(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "storage.agent_memory._AGENT_MEM_DIR", tmp_path / "agent_memory"
    )
    # Write malformed content directly.
    path = tmp_path / "agent_memory" / "dev" / "weather.json"
    path.parent.mkdir(parents=True)
    path.write_text("not valid json {")
    assert load_agent_memory("dev", "weather") == {}

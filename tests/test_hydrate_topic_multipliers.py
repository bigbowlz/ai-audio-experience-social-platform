"""hydrate_topic_multipliers tests — TOML parse, category expansion, override,
clamping, seed seam.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from learning_loop.hydrate_topic_multipliers import (
    TOPIC_MULTIPLIER_MAX,
    TOPIC_MULTIPLIER_MIN,
    hydrate_topic_multipliers,
    load_config,
    resolve_weights,
)


# ── load_config ──


def test_load_config_missing_file(tmp_path: Path):
    assert load_config(tmp_path / "does_not_exist.toml") == {}


def test_load_config_parses_toml(tmp_path: Path):
    p = tmp_path / "w.toml"
    p.write_text('[weights.youtube]\n"rock-music" = 0.5\n')
    cfg = load_config(p)
    assert cfg == {"weights": {"youtube": {"rock-music": 0.5}}}


# ── resolve_weights ──


def test_resolve_weights_empty_agent_returns_empty():
    assert resolve_weights({}, "youtube") == {}
    assert resolve_weights({"weights": {"external": {"pop-music": 0.3}}}, "youtube") == {}


def test_resolve_weights_expands_category():
    cfg = {
        "categories": {"music": ["rock-music", "pop-music", "jazz"]},
        "weights": {"youtube": {"music": 0.5}},
    }
    assert resolve_weights(cfg, "youtube") == {
        "rock-music": 0.5,
        "pop-music": 0.5,
        "jazz": 0.5,
    }


def test_resolve_weights_per_topic_override_wins():
    cfg = {
        "categories": {"music": ["rock-music", "pop-music", "jazz"]},
        "weights": {"youtube": {"music": 0.5, "jazz": 0.2}},
    }
    resolved = resolve_weights(cfg, "youtube")
    assert resolved["rock-music"] == 0.5
    assert resolved["pop-music"] == 0.5
    assert resolved["jazz"] == 0.2  # override beats category


def test_resolve_weights_specific_topic_without_category():
    cfg = {"weights": {"youtube": {"action-game": 0.7}}}
    assert resolve_weights(cfg, "youtube") == {"action-game": 0.7}


def test_resolve_weights_clamps_to_max():
    cfg = {"weights": {"youtube": {"rock-music": 100.0}}}
    assert resolve_weights(cfg, "youtube")["rock-music"] == TOPIC_MULTIPLIER_MAX


def test_resolve_weights_clamps_to_min():
    cfg = {"weights": {"youtube": {"rock-music": 0.0}}}
    assert resolve_weights(cfg, "youtube")["rock-music"] == TOPIC_MULTIPLIER_MIN


def test_resolve_weights_identity_at_1_0():
    cfg = {"weights": {"youtube": {"rock-music": 1.0}}}
    assert resolve_weights(cfg, "youtube") == {"rock-music": 1.0}


def test_resolve_weights_per_agent_isolation():
    cfg = {
        "categories": {"music": ["rock-music"]},
        "weights": {
            "youtube":  {"music": 0.5},
            "external": {"music": 0.3},
        },
    }
    assert resolve_weights(cfg, "youtube")  == {"rock-music": 0.5}
    assert resolve_weights(cfg, "external") == {"rock-music": 0.3}


# ── hydrate_topic_multipliers ──


def test_hydrate_missing_file_returns_empty(tmp_path: Path):
    result = hydrate_topic_multipliers("dev", path=tmp_path / "absent.toml")
    assert result == {}


def test_hydrate_calls_seed_per_agent(tmp_path: Path, monkeypatch):
    p = tmp_path / "w.toml"
    p.write_text(
        '[categories]\n'
        'music = ["rock-music", "pop-music"]\n'
        '\n'
        '[weights.youtube]\n'
        'music = 0.5\n'
        '\n'
        '[weights.external]\n'
        'music = 0.3\n'
    )
    seed = mock.Mock()
    monkeypatch.setattr(
        "learning_loop.hydrate_topic_multipliers.seed_topic_multiplier", seed
    )
    result = hydrate_topic_multipliers("dev", path=p)

    assert seed.call_count == 2
    calls = {args[1]: args[2] for args, _ in seed.call_args_list}  # {agent: weights}
    assert calls["youtube"]  == {"rock-music": 0.5, "pop-music": 0.5}
    assert calls["external"] == {"rock-music": 0.3, "pop-music": 0.3}
    assert result == calls


def test_hydrate_returns_empty_when_no_weights_section(tmp_path: Path, monkeypatch):
    p = tmp_path / "w.toml"
    p.write_text('[categories]\nmusic = ["rock-music"]\n')  # no [weights]
    seed = mock.Mock()
    monkeypatch.setattr(
        "learning_loop.hydrate_topic_multipliers.seed_topic_multiplier", seed
    )
    assert hydrate_topic_multipliers("dev", path=p) == {}
    seed.assert_not_called()

"""Tests for the producer segment-script cache surface (news-narration spec §3)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from producer import DEFAULT_CACHE_DIR, cache_dir


class TestCacheDirResolver:
    def test_default_is_tmp_segment_script_cache(self):
        assert DEFAULT_CACHE_DIR == Path("tmp/segment_script_cache")

    def test_resolver_returns_default_when_env_absent(self, monkeypatch):
        monkeypatch.delenv("RADIO_CACHE_DIR", raising=False)
        assert cache_dir() == DEFAULT_CACHE_DIR

    def test_resolver_reads_env_override(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))
        assert cache_dir() == tmp_path

    def test_resolver_reads_each_call(self, monkeypatch, tmp_path: Path):
        """No import-time capture — env changes reflect immediately."""
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path / "a"))
        assert cache_dir() == tmp_path / "a"
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path / "b"))
        assert cache_dir() == tmp_path / "b"

    def test_resolver_falls_back_on_empty_string(self, monkeypatch):
        monkeypatch.setenv("RADIO_CACHE_DIR", "")
        assert cache_dir() == DEFAULT_CACHE_DIR


from producer.script import _segment_cache_path, _slug_title


class TestSlugTitle:
    def test_lowercases(self):
        assert _slug_title("Jazz Exploration") == "jazz_exploration"

    def test_replaces_non_alphanumerics(self):
        assert _slug_title("Bach's B-minor Mass!") == "bach_s_b_minor_mass"

    def test_collapses_runs(self):
        assert _slug_title("foo   bar   baz") == "foo_bar_baz"
        assert _slug_title("foo-_-bar") == "foo_bar"

    def test_trims_edges(self):
        assert _slug_title("   jazz   ") == "jazz"
        assert _slug_title("!!!jazz!!!") == "jazz"

    def test_handles_unicode_and_digits(self):
        assert _slug_title("Café 2026 — Édition") == "caf_2026_dition"

    def test_empty_or_all_punctuation_returns_underscore(self):
        # Must never produce an empty string (would break the filename).
        assert _slug_title("") == "_"
        assert _slug_title("!!!") == "_"


class TestSegmentCachePath:
    def test_path_shape(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))
        got = _segment_cache_path("youtube", "Jazz Exploration", "2026-04-18", 130)
        assert got == tmp_path / "segment_scripts" / "youtube_jazz_exploration_20260418_130.json"

    def test_strips_date_dashes(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("RADIO_CACHE_DIR", str(tmp_path))
        got = _segment_cache_path("alices", "PG Essay", "2026-04-18", 130)
        # YYYYMMDD, not YYYY-MM-DD
        assert "20260418" in got.name
        assert "2026-04-18" not in got.name

    def test_uses_cache_dir_default_when_env_absent(self, monkeypatch):
        monkeypatch.delenv("RADIO_CACHE_DIR", raising=False)
        got = _segment_cache_path("youtube", "x", "2026-04-18", 130)
        assert got == Path("tmp/segment_script_cache/segment_scripts/youtube_x_20260418_130.json")


from producer.script import (
    SegmentScript,
    _read_cached_segment,
    _write_cached_artifact,
)


def _artifact_dict(**segment_overrides) -> dict:
    seg = {
        "agent": "youtube",
        "pitch_title": "Jazz Exploration",
        "segue_in": "And next —",
        "script": "This is a sufficiently long script body to pass the floor.",
        "estimated_length_sec": 60,
    }
    seg.update(segment_overrides)
    return {
        "segment": seg,
        "debug": {
            "search_query": "jazz",
            "search_used": True,
            "broadened": False,
            "research_outcome": "story",
            "raw_llm_text": "...",
            "input_pitch": {"title": "Jazz Exploration", "hook": "h",
                            "source_refs": [], "claim_kind": "neutral"},
            "target_words": 130,
            "words_per_minute": 130,
        },
    }


class TestWriteCachedArtifact:
    def test_writes_pretty_json(self, tmp_path: Path):
        path = tmp_path / "seg.json"
        art = _artifact_dict()
        _write_cached_artifact(path, art["segment"], art["debug"])
        raw = path.read_text(encoding="utf-8")
        # Pretty-printed → at least one indented line
        assert "\n  " in raw
        loaded = json.loads(raw)
        assert loaded["segment"]["agent"] == "youtube"
        assert loaded["debug"]["search_query"] == "jazz"

    def test_creates_parent_dirs(self, tmp_path: Path):
        path = tmp_path / "does" / "not" / "exist" / "seg.json"
        art = _artifact_dict()
        _write_cached_artifact(path, art["segment"], art["debug"])
        assert path.exists()

    def test_atomic_no_partial_file_on_failure(self, tmp_path: Path, monkeypatch):
        """If os.replace raises, the final path must not exist."""
        path = tmp_path / "seg.json"

        def boom(src, dst):
            raise OSError("simulated rename failure")

        monkeypatch.setattr("os.replace", boom)

        with pytest.raises(OSError):
            art = _artifact_dict()
            _write_cached_artifact(path, art["segment"], art["debug"])
        assert not path.exists()
        # No leftover *.tmp either
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []


class TestReadCachedSegment:
    def test_returns_segment_on_hit(self, tmp_path: Path):
        path = tmp_path / "seg.json"
        art = _artifact_dict()
        path.write_text(json.dumps(art), encoding="utf-8")
        got = _read_cached_segment(path)
        assert got is not None
        assert got["agent"] == "youtube"
        assert got["pitch_title"] == "Jazz Exploration"
        assert got["script"].startswith("This is a sufficiently long")

    def test_returns_none_when_missing(self, tmp_path: Path):
        assert _read_cached_segment(tmp_path / "nope.json") is None

    def test_soft_fails_on_malformed_json(self, tmp_path: Path, capsys):
        path = tmp_path / "seg.json"
        path.write_text("not json at all {{{", encoding="utf-8")
        # Must NOT raise — soft-fail, log, return None (spec §3: cache is advisory).
        assert _read_cached_segment(path) is None

    def test_soft_fails_on_missing_segment_key(self, tmp_path: Path):
        path = tmp_path / "seg.json"
        path.write_text(json.dumps({"debug": {}}), encoding="utf-8")
        assert _read_cached_segment(path) is None

    def test_soft_fails_on_missing_required_segment_field(self, tmp_path: Path):
        art = _artifact_dict()
        del art["segment"]["script"]
        path = tmp_path / "seg.json"
        path.write_text(json.dumps(art), encoding="utf-8")
        assert _read_cached_segment(path) is None

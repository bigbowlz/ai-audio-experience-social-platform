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

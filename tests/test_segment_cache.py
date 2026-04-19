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

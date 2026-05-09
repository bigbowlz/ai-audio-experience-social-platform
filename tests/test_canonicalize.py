"""Tests for agents/youtube/canonicalize.py.

Network calls are mocked via monkeypatch on `_query_batch`. Cache I/O hits a
tmp_path so tests don't touch the committed snapshot.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.youtube import canonicalize as canon


# ── URL/title normalization ──────────────────────────────────────────


class TestToCanonicalUrl:
    def test_full_url_passes_through(self):
        u = "https://en.wikipedia.org/wiki/Rock_music"
        assert canon._to_canonical_url(u) == u

    def test_http_upgraded_to_https(self):
        u = "http://en.wikipedia.org/wiki/Rock_music"
        assert canon._to_canonical_url(u).startswith("https://")

    def test_bare_title_expanded(self):
        assert canon._to_canonical_url("Rock_music") == "https://en.wikipedia.org/wiki/Rock_music"

    def test_bare_title_with_space(self):
        # spaces normalize to underscores
        assert canon._to_canonical_url("Rock music") == "https://en.wikipedia.org/wiki/Rock_music"


class TestUrlToTitle:
    def test_simple(self):
        assert canon._url_to_title("https://en.wikipedia.org/wiki/Rock_music") == "Rock_music"

    def test_url_decoded(self):
        assert canon._url_to_title("https://en.wikipedia.org/wiki/Caf%C3%A9") == "Café"


# ── Cache I/O ────────────────────────────────────────────────────────


class TestCacheIO:
    def test_load_missing_returns_empty(self, tmp_path):
        cache = canon._load_cache(tmp_path / "nonexistent.json")
        assert cache == {"url_to_qid": {}, "qid_to_meta": {}}

    def test_save_then_load_roundtrip(self, tmp_path):
        path = tmp_path / "cache.json"
        cache = {
            "url_to_qid": {"https://en.wikipedia.org/wiki/Foo": "Q1"},
            "qid_to_meta": {"Q1": {"label": "Foo", "canonical_url": "..."}},
        }
        canon._save_cache(path, cache)
        loaded = canon._load_cache(path)
        assert loaded == cache

    def test_atomic_write_no_partial_file_on_interrupt(self, tmp_path, monkeypatch):
        """Simulate a crash mid-write — the existing file should remain untouched."""
        path = tmp_path / "cache.json"
        # Seed with valid contents
        original = {"url_to_qid": {"a": "Q1"}, "qid_to_meta": {"Q1": {"label": "x", "canonical_url": "y"}}}
        canon._save_cache(path, original)

        # Make json.dump explode mid-write
        import json as _json
        real_dump = _json.dump

        def boom(*args, **kwargs):
            raise RuntimeError("simulated crash")

        monkeypatch.setattr(_json, "dump", boom)
        with pytest.raises(RuntimeError):
            canon._save_cache(path, {"url_to_qid": {"changed": "Q2"}, "qid_to_meta": {}})

        # Original survives
        loaded = canon._load_cache(path)
        assert loaded == original


# ── canonicalize() with mocked API ───────────────────────────────────


def _mock_query(mapping: dict[str, tuple[str | None, str]]):
    """Return a fake _query_batch that resolves from a fixed mapping."""
    def fake(titles):
        return {t: mapping.get(t, (None, t)) for t in titles}
    return fake


class TestCanonicalize:
    def test_cache_hit_no_network(self, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        canon._save_cache(cache_path, {
            "url_to_qid": {"https://en.wikipedia.org/wiki/Rock_music": "Q11399"},
            "qid_to_meta": {
                "Q11399": {"label": "Rock music", "canonical_url": "https://en.wikipedia.org/wiki/Rock_music"}
            },
        })

        # Any network call would be a bug
        def boom(_titles):
            raise AssertionError("network should not be called for cache hits")
        monkeypatch.setattr(canon, "_query_batch", boom)

        out = canon.canonicalize(
            ["https://en.wikipedia.org/wiki/Rock_music"], cache_path=cache_path
        )
        assert out["https://en.wikipedia.org/wiki/Rock_music"]["qid"] == "Q11399"
        assert out["https://en.wikipedia.org/wiki/Rock_music"]["label"] == "Rock music"

    def test_cache_miss_populates(self, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        monkeypatch.setattr(canon, "_query_batch", _mock_query({
            "Rock_music": ("Q11399", "Rock_music"),
        }))
        out = canon.canonicalize(
            ["https://en.wikipedia.org/wiki/Rock_music"], cache_path=cache_path
        )
        assert out["https://en.wikipedia.org/wiki/Rock_music"]["qid"] == "Q11399"

        # Cache should now contain this entry
        cache = json.loads(cache_path.read_text())
        assert cache["url_to_qid"]["https://en.wikipedia.org/wiki/Rock_music"] == "Q11399"
        assert "Q11399" in cache["qid_to_meta"]

    def test_redirect_collapses_synonyms(self, tmp_path, monkeypatch):
        """Two different URLs whose API resolution lands on the same QID share metadata."""
        cache_path = tmp_path / "cache.json"
        monkeypatch.setattr(canon, "_query_batch", _mock_query({
            "Soccer": ("Q2736", "Association_football"),
            "Association_football": ("Q2736", "Association_football"),
        }))
        out = canon.canonicalize([
            "https://en.wikipedia.org/wiki/Soccer",
            "https://en.wikipedia.org/wiki/Association_football",
        ], cache_path=cache_path)
        assert out["https://en.wikipedia.org/wiki/Soccer"]["qid"] == "Q2736"
        assert out["https://en.wikipedia.org/wiki/Association_football"]["qid"] == "Q2736"
        # Same QID, same metadata
        assert out["https://en.wikipedia.org/wiki/Soccer"] == out[
            "https://en.wikipedia.org/wiki/Association_football"
        ]

    def test_no_qid_resolution(self, tmp_path, monkeypatch):
        """Page with no Wikidata link returns None."""
        cache_path = tmp_path / "cache.json"
        monkeypatch.setattr(canon, "_query_batch", _mock_query({
            "Imaginary_page": (None, "Imaginary_page"),
        }))
        out = canon.canonicalize(
            ["https://en.wikipedia.org/wiki/Imaginary_page"], cache_path=cache_path
        )
        assert out["https://en.wikipedia.org/wiki/Imaginary_page"] is None

        # Negative result is cached so we don't re-query
        cache = json.loads(cache_path.read_text())
        assert cache["url_to_qid"]["https://en.wikipedia.org/wiki/Imaginary_page"] is None

    def test_bare_title_input(self, tmp_path, monkeypatch):
        """Inputs from 08_video_topic_details.json are bare titles, not URLs."""
        cache_path = tmp_path / "cache.json"
        monkeypatch.setattr(canon, "_query_batch", _mock_query({
            "Rock_music": ("Q11399", "Rock_music"),
        }))
        out = canon.canonicalize(["Rock_music"], cache_path=cache_path)
        assert out["Rock_music"]["qid"] == "Q11399"

    def test_batching(self, tmp_path, monkeypatch):
        """More than 50 inputs → multiple batched calls."""
        cache_path = tmp_path / "cache.json"
        # Generate 75 unique titles
        urls = [f"https://en.wikipedia.org/wiki/T{i}" for i in range(75)]
        mapping = {f"T{i}": (f"Q{i}", f"T{i}") for i in range(75)}

        call_sizes: list[int] = []
        def counting_query(titles):
            call_sizes.append(len(titles))
            return {t: mapping[t] for t in titles}

        monkeypatch.setattr(canon, "_query_batch", counting_query)
        out = canon.canonicalize(urls, cache_path=cache_path)
        assert all(out[u] is not None for u in urls)
        # 75 titles split as 50 + 25
        assert call_sizes == [50, 25]

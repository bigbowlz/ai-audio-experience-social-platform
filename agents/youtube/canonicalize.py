"""Wikipedia URL/title → Wikidata QID canonicalization with disk-backed cache.

Resolves YouTube `topicCategories` URLs (and bare page-name tags from
`08_video_topic_details.json`) to stable Wikidata QIDs via the MediaWiki API,
collapsing redirects and synonyms. The QID is the canonical topic identity;
URLs and titles drift, QIDs don't.

Cache layout — single JSON file with two top-level dicts:

    {
      "url_to_qid": { "<canonical wiki url>": "<qid or null>", ... },
      "qid_to_meta": { "<qid>": {"label": ..., "canonical_url": ...}, ... }
    }

Hot path on every profile build is `url_to_qid` (direct dict lookup). The
`qid_to_meta` map is one entry per real topic; multiple URL aliases collapse
to the same QID and share one metadata entry.

Public API:
    canonicalize(inputs, cache_path=None) -> dict[str, CanonicalTopic | None]
    bootstrap(probe_dirs, cache_path=None) -> int     # CLI entry point

Standalone:
    python -m agents.youtube.canonicalize --bootstrap
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import TypedDict
from urllib.parse import quote, unquote

import requests


_REPO_ROOT = Path(__file__).parents[2]
_DEFAULT_CACHE = Path(__file__).parent / "topic_cache.json"
_WIKI_PREFIX = "https://en.wikipedia.org/wiki/"
_API = "https://en.wikipedia.org/w/api.php"
_BATCH = 50
_RETRY_DELAY_S = 2.0
_MAX_RETRIES = 3


class CanonicalTopic(TypedDict):
    qid: str
    label: str
    canonical_url: str


# ── URL / title normalization ────────────────────────────────────────


def _to_canonical_url(input_str: str) -> str:
    """Normalize either a URL or a bare page name to a canonical Wikipedia URL.

    Both `https://en.wikipedia.org/wiki/Rock_music` and `Rock_music` map to
    `https://en.wikipedia.org/wiki/Rock_music`. URL-encoding is preserved so
    that "Caf%C3%A9" stays as-is (decoded form would lose roundtrip safety).
    """
    if input_str.startswith("http://") or input_str.startswith("https://"):
        return input_str.replace("http://", "https://", 1)
    # bare title — strip leading slash if any, ensure underscored
    title = input_str.lstrip("/").replace(" ", "_")
    # URL-encode to match what the Wiki API will normalize titles into
    return _WIKI_PREFIX + quote(title, safe="_(),'")


def _url_to_title(url: str) -> str:
    """Extract the page title (URL-decoded, with underscores) from a wiki URL."""
    segment = url.rsplit("/", 1)[-1]
    return unquote(segment)


# ── Cache I/O ────────────────────────────────────────────────────────


def _load_cache(path: Path) -> dict:
    if not path.exists():
        return {"url_to_qid": {}, "qid_to_meta": {}}
    with open(path) as f:
        data = json.load(f)
    data.setdefault("url_to_qid", {})
    data.setdefault("qid_to_meta", {})
    return data


def _save_cache(path: Path, cache: dict) -> None:
    """Atomic write: serialize to .tmp then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


# ── MediaWiki API ────────────────────────────────────────────────────


def _query_batch(titles: list[str]) -> dict[str, tuple[str | None, str]]:
    """Resolve a batch of up to 50 titles to (qid, canonical_title).

    Returns a dict keyed by the *input* title. Resolution chases redirects
    (page A → page B) and normalization (case/underscore fixes) automatically.
    qid is None if the page is missing or has no Wikidata link.
    """
    params = {
        "action": "query",
        "format": "json",
        "prop": "pageprops",
        "ppprop": "wikibase_item",
        "redirects": 1,
        "titles": "|".join(titles),
    }
    last_err: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            r = requests.get(_API, params=params, timeout=15.0,
                             headers={"User-Agent": "radio-podcast/0.1 canonicalize"})
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:
            last_err = e
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAY_S * (attempt + 1))
            else:
                raise
    else:
        raise last_err  # type: ignore[misc]

    query = data.get("query", {})

    # Walk the chain: input → normalized → redirected → canonical title
    chain: dict[str, str] = {t: t for t in titles}
    for step in query.get("normalized", []):
        for k, v in chain.items():
            if v == step["from"]:
                chain[k] = step["to"]
    # Apply redirects iteratively (a page may redirect through multiple hops)
    for _ in range(5):
        changed = False
        for step in query.get("redirects", []):
            for k, v in chain.items():
                if v == step["from"]:
                    chain[k] = step["to"]
                    changed = True
        if not changed:
            break

    # Build title → qid from query.pages
    title_to_qid: dict[str, str | None] = {}
    for _pageid, page in query.get("pages", {}).items():
        title = page.get("title", "")
        # Missing pages have a "missing" key; no pageprops at all
        if "missing" in page:
            title_to_qid[title] = None
            continue
        qid = page.get("pageprops", {}).get("wikibase_item")
        title_to_qid[title] = qid  # may be None if no Wikidata link

    out: dict[str, tuple[str | None, str]] = {}
    for input_title in titles:
        canonical = chain.get(input_title, input_title)
        # MediaWiki normalizes spaces to underscores in pages.title — but the
        # redirect/normalized chains use spaces. Try both forms.
        qid = title_to_qid.get(canonical)
        if qid is None:
            qid = title_to_qid.get(canonical.replace("_", " "))
        if qid is None:
            qid = title_to_qid.get(canonical.replace(" ", "_"))
        out[input_title] = (qid, canonical)
    return out


# ── Public canonicalize() ────────────────────────────────────────────


def canonicalize(
    inputs: list[str],
    cache_path: Path | None = None,
) -> dict[str, CanonicalTopic | None]:
    """Resolve a batch of Wikipedia URLs/titles to QID + metadata.

    Cache-first: known inputs are served from the disk cache without any
    network calls. Unknown inputs are batched (50 per request) against the
    MediaWiki API, then persisted back to the cache atomically.

    Args:
        inputs: list of full Wikipedia URLs (`https://en.wikipedia.org/wiki/X`)
            or bare page names (`X`). Mixed forms allowed.
        cache_path: cache file location; defaults to topic_cache.json next
            to this module.

    Returns:
        dict keyed by *input string as given* (so callers can map back from
        their original URL/title list). Values are CanonicalTopic dicts or
        None if the page has no Wikidata entity.
    """
    cache_path = cache_path or _DEFAULT_CACHE
    cache = _load_cache(cache_path)
    url_to_qid: dict[str, str | None] = cache["url_to_qid"]
    qid_to_meta: dict[str, dict] = cache["qid_to_meta"]

    # Map each input to its canonical URL form for cache lookup
    input_to_url = {s: _to_canonical_url(s) for s in inputs}

    # Find cache misses, deduped by canonical URL
    missing_urls: list[str] = []
    for url in set(input_to_url.values()):
        if url not in url_to_qid:
            missing_urls.append(url)

    # Batch-resolve cache misses
    if missing_urls:
        title_to_url = {_url_to_title(u): u for u in missing_urls}
        titles = list(title_to_url.keys())
        for i in range(0, len(titles), _BATCH):
            chunk = titles[i : i + _BATCH]
            results = _query_batch(chunk)
            for input_title, (qid, canonical_title) in results.items():
                input_url = title_to_url[input_title]
                url_to_qid[input_url] = qid
                if qid is not None and qid not in qid_to_meta:
                    canonical_url = (
                        _WIKI_PREFIX + quote(canonical_title.replace(" ", "_"),
                                             safe="_(),'")
                    )
                    qid_to_meta[qid] = {
                        "label": canonical_title.replace("_", " "),
                        "canonical_url": canonical_url,
                    }
            # Persist after each batch so partial progress survives interruption
            _save_cache(cache_path, cache)

    # Build the output: one entry per input string
    out: dict[str, CanonicalTopic | None] = {}
    for input_str, url in input_to_url.items():
        qid = url_to_qid.get(url)
        if qid is None:
            out[input_str] = None
            continue
        meta = qid_to_meta.get(qid)
        if meta is None:
            out[input_str] = None
            continue
        out[input_str] = CanonicalTopic(
            qid=qid,
            label=meta["label"],
            canonical_url=meta["canonical_url"],
        )
    return out


# ── Bootstrap entry point ────────────────────────────────────────────


def _collect_urls_from_capture(probe_dir: Path) -> set[str]:
    """Walk one capture directory and collect all unique topic URLs/titles."""
    urls: set[str] = set()
    chan_path = probe_dir / "07_topic_details.json"
    if chan_path.exists():
        with open(chan_path) as f:
            chan_raw = json.load(f)
        for item in chan_raw.get("items", []):
            for u in item.get("topicDetails", {}).get("topicCategories", []):
                urls.add(u)
    vid_path = probe_dir / "08_video_topic_details.json"
    if vid_path.exists():
        with open(vid_path) as f:
            vid_raw = json.load(f)
        for entry in vid_raw.get("per_video", []):
            for t in entry.get("tags", []):
                urls.add(t)
    return urls


def bootstrap(
    probe_dirs: list[Path] | None = None,
    cache_path: Path | None = None,
) -> int:
    """One-time bulk canonicalization of every URL across known capture dirs.

    Returns the number of unique URLs/titles processed.
    """
    if probe_dirs is None:
        probe_dirs = [
            _REPO_ROOT / "ydata" / "user",
            _REPO_ROOT / "ydata" / "guest",
            _REPO_ROOT / "agents" / "external" / "data",
        ]
    all_urls: set[str] = set()
    for d in probe_dirs:
        if d.exists():
            n_before = len(all_urls)
            all_urls.update(_collect_urls_from_capture(d))
            print(f"  {d}: +{len(all_urls) - n_before} unique")
    print(f"Total unique URLs/titles: {len(all_urls)}")
    if not all_urls:
        return 0
    canonicalize(sorted(all_urls), cache_path=cache_path)
    print(f"Cache written to {cache_path or _DEFAULT_CACHE}")
    return len(all_urls)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bootstrap or query the Wikipedia → Wikidata QID cache."
    )
    parser.add_argument(
        "--bootstrap", action="store_true",
        help="Walk all capture dirs and pre-populate the cache.",
    )
    parser.add_argument(
        "--cache", type=Path, default=_DEFAULT_CACHE,
        help=f"Cache file path (default: {_DEFAULT_CACHE})",
    )
    parser.add_argument(
        "--probe-dirs", type=Path, nargs="*",
        help="Override capture dirs (default: ydata/user, ydata/guest, agents/external/data).",
    )
    args = parser.parse_args()

    if args.bootstrap:
        bootstrap(args.probe_dirs, args.cache)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
